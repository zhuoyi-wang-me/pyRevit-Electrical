# -*- coding: utf-8 -*-
"""Fill Spare/Space -- interactive two-step pyRevit tool.

STEP 1 (ui_select.xaml / SelectWindow): the project's panel schedules are
listed with their level, distribution system, supply and how full they are.
Filter the list, tick the panels to update.

STEP 2 (ui_config.xaml / ConfigWindow): for each ticked panel, review / edit
the Spare Needed, Space Needed, Spare Rating and Poles (pre-filled from the
Ways table and Panel-type table of Step 1), then click Fill / Update.

THIS TOOL MODIFIES THE MODEL (panel schedules + the panel's number of ways).
Every panel is its own transaction, so one panel Revit refuses does not undo
the others; the whole run is one entry in Revit's Undo list.
"""

import io
import json
import os
import re
import time
import traceback
from math import ceil

import clr
clr.AddReference("WindowsBase")
clr.AddReference("PresentationCore")
clr.AddReference("PresentationFramework")
from System.Collections.ObjectModel import ObservableCollection
from System.Windows import RoutedEventHandler, Visibility
from System.Windows.Controls import DataGridEditingUnit
from System.Windows.Controls.Primitives import ButtonBase
from System.Windows.Input import Key
from System.Windows.Threading import Dispatcher, DispatcherPriority, DispatcherFrame
import System

from pyrevit import revit, DB, forms, script

doc = revit.doc
uidoc = revit.uidoc
logger = script.get_logger()

SELECT_XAML = script.get_bundle_file("ui_select.xaml")
CONFIG_XAML = script.get_bundle_file("ui_config.xaml")

SETTINGS_PATH = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")),
                             "EleTools", "FillSpares", "settings.json")


# ---------------------------------------------------------------------------
# Defaults (shown in the editable Step 1 tables; used to prefill Step 2's
# per-panel suggestions -- everything stays editable per panel in Step 2).
# ---------------------------------------------------------------------------
DEFAULT_WAYS_TABLE = [
    (12, 2, 2), (18, 2, 2), (24, 3, 3), (30, 3, 3),
    (36, 4, 4), (42, 5, 5), (48, 5, 5), (54, 6, 6),
]

DEFAULT_TYPE_CONFIG = [
    ("PP", 20, 1),
    ("LP", 20, 1),
    ("ELP", 20, 1),
    ("UDB", 30, 1),
    ("ACP", 30, 2),
]
DEFAULT_FALLBACK_RATING = 20
DEFAULT_FALLBACK_POLES = 1
OTHER_TYPE = "Other"

DEFAULT_OPTIONS = {
    "remove_existing": True,     # delete the Spare/Space already in the schedule first
    "resize": True,              # set the panel's number of ways to the standard size
    "fill_leftover": True,       # mark every way that is still empty as Space
}

# The panel's "number of ways" parameter. Revit has renamed it over the years
# (and switchboards use a different one), so the built-in parameter is tried
# first and then each display name.
CAPACITY_BUILTINS = ["RBS_ELEC_MAX_POLE_BREAKERS", "RBS_ELEC_NUMBER_OF_CIRCUITS"]
CAPACITY_PARAM_NAMES = [
    "Max Number of Single Pole Breakers",  # panelboards, current Revit
    "Max #1 Pole Breakers",                # panelboards, older Revit
    "Max Number of Circuits",              # switchboards, current Revit
]

ALL = "(all)"
SHOW_ALL = "All panels"
SHOW_TICKED = "Ticked only"
SHOW_UNTICKED = "Not ticked"
SHOW_FREE = "With empty ways"
SHOW_NO_PLACEHOLDER = "No spare or space yet"
SHOW_REVIT = "From Revit selection"
SHOW_CHOICES = [SHOW_ALL, SHOW_TICKED, SHOW_UNTICKED, SHOW_FREE, SHOW_NO_PLACEHOLDER, SHOW_REVIT]

BAR_WIDTH = 120.0     # pixels of the "how full" bar in the Step 1 grid


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def eid_value(element_id):
    """ElementId -> integer. ElementId.IntegerValue no longer exists in Revit
    2026, ElementId.Value does not exist before Revit 2024."""
    try:
        return int(element_id.Value)
    except AttributeError:
        return int(element_id.IntegerValue)


def to_int(value, name="value", minimum=0, maximum=None):
    """Text typed in a grid cell -> int, with a readable error."""
    try:
        number = int(float(str(value).strip()))
    except Exception:
        raise ValueError("{} '{}' is not a number".format(name, value))
    if number < minimum or (maximum is not None and number > maximum):
        raise ValueError("{} must be {}{}".format(
            name, "at least {}".format(minimum) if maximum is None
            else "between {} and {}".format(minimum, maximum), ""))
    return number


def builtin(name):
    return getattr(DB.BuiltInParameter, name, None)


def param_text(element, builtin_name):
    """Text of a built-in parameter, '' when it is missing or empty."""
    bip = builtin(builtin_name)
    if element is None or bip is None:
        return ""
    try:
        p = element.get_Parameter(bip)
        if p is None:
            return ""
        return (p.AsString() or p.AsValueString() or "").strip()
    except Exception:
        return ""


def load_settings():
    try:
        with io.open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_settings(data):
    try:
        folder = os.path.dirname(SETTINGS_PATH)
        if not os.path.isdir(folder):
            os.makedirs(folder)
        with io.open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            f.write(unicode(json.dumps(data, indent=2)))
    except Exception:
        logger.debug(traceback.format_exc())


def themed_xaml(path):
    """Step 2 shares Step 1's colours and control styles: the {{RESOURCES}}
    token in ui_config.xaml is replaced by Step 1's <Window.Resources>."""
    with io.open(SELECT_XAML, "r", encoding="utf-8") as f:
        source = f.read()
    with io.open(path, "r", encoding="utf-8") as f:
        target = f.read()
    found = re.search(r"<Window\.Resources>(.*?)</Window\.Resources>", source, re.S)
    return target.replace("{{RESOURCES}}", found.group(1) if found else "")


def commit_edits(grid):
    """Finish any cell / row that is still being edited. Without this,
    Items.Refresh() throws ('not allowed during an AddNew or EditItem
    transaction') and the button that was clicked appears to do nothing."""
    try:
        grid.CommitEdit(DataGridEditingUnit.Cell, True)
        grid.CommitEdit(DataGridEditingUnit.Row, True)
    except Exception:
        pass


def refresh_grid(grid):
    commit_edits(grid)
    try:
        grid.Items.Refresh()
    except Exception:
        try:
            grid.CancelEdit()
            grid.Items.Refresh()
        except Exception:
            logger.debug(traceback.format_exc())


def pump_ui():
    """WPF's version of DoEvents() -- processes pending UI messages so the
    window repaints and doesn't get flagged 'Not Responding' during a
    longer synchronous loop."""
    frame = DispatcherFrame()

    def _exit_frame(*args):
        frame.Continue = False
        return None

    Dispatcher.CurrentDispatcher.BeginInvoke(
        DispatcherPriority.Background, System.Action(_exit_frame))
    Dispatcher.PushFrame(frame)


# ---------------------------------------------------------------------------
# Row classes bound to the DataGrids (plain attributes -- IronPython objects
# support WPF's reflection-based binding).
# ---------------------------------------------------------------------------
class WaysRow(object):
    def __init__(self, ways, spare, space):
        self.Ways = ways
        self.Spare = spare
        self.Space = space


class TypeConfigRow(object):
    def __init__(self, prefix, spare_rating, poles):
        self.Prefix = prefix
        self.SpareRating = spare_rating
        self.Poles = poles


class PanelPickRow(object):
    """Step 1 grid row: one panel schedule and how full it is."""
    def __init__(self, element_id, name, prefix, level, system, supply, ways, stats):
        self.Selected = False
        self.Name = name
        self.Type = prefix
        self.Level = level
        self.System = system
        self.SupplyFrom = supply
        self.Ways = ways
        self.IsPicked = False          # is in the current Revit selection
        self._element_id = element_id
        self.set_stats(stats)

    def set_stats(self, stats):
        self._stats = stats
        if stats is None:
            # Not read yet: the ways are read after the window is on screen,
            # because that is the slow part of opening the tool.
            self.Connected = self.Spares = self.Spaces = self.Free = ""
            self.Usage = "reading..."
            self.ConnPx = self.SparePx = self.SpacePx = self.FreePx = 0.0
            return
        self.Connected = stats["connected"]
        self.Spares = stats["spares"]
        self.Spaces = stats["spaces"]
        self.Free = stats["free"]
        self.Usage = "{} / {}".format(self.Ways - self.Free, self.Ways)
        total = float(max(1, stats["connected"] + stats["spare_slots"]
                          + stats["space_slots"] + stats["free"]))
        self.ConnPx = BAR_WIDTH * stats["connected"] / total
        self.SparePx = BAR_WIDTH * stats["spare_slots"] / total
        self.SpacePx = BAR_WIDTH * stats["space_slots"] / total
        self.FreePx = BAR_WIDTH * stats["free"] / total


def load_row_stats(row):
    """Read how the ways of one Step 1 row are used (the slow part: two Revit
    calls per way). Does nothing when the row already has them."""
    if row._stats is not None:
        return
    try:
        psv = doc.GetElement(DB.ElementId(row._element_id))
        row.set_stats(slot_stats(psv, row.Ways))
    except Exception:
        logger.debug(traceback.format_exc())
        row.set_stats({"connected": 0, "spare_slots": 0, "space_slots": 0, "free": 0,
                       "spares": 0, "spaces": 0, "last_real": 0, "last_any": 0})
        row.Usage = "could not be read"


class PanelConfigRow(object):
    """Step 2 grid row -- the values that will be applied, editable per panel."""
    def __init__(self, pick, spare_needed, space_needed, spare_rating, poles):
        self.Include = True
        self.Name = pick.Name
        self.Type = pick.Type
        self.ConnectedWays = pick.Connected
        self.WaysNow = pick.Ways
        self.WaysAfter = pick.Ways
        self.SpareNeeded = spare_needed
        self.SpaceNeeded = space_needed
        self.SpareRating = spare_rating
        self.Poles = poles
        self.Status = "Pending"
        self.StatusKind = "pending"      # pending / ok / warn / error
        self._element_id = pick._element_id
        self._stats = pick._stats


# ---------------------------------------------------------------------------
# Reading a panel schedule
# ---------------------------------------------------------------------------
def placeholder_kind(circuit):
    """'spare', 'space' or None (a real circuit / nothing).

    Revit's own CircuitType is used when it is there, so a spare that was
    renamed is still a spare and a real circuit somebody named SPARE is still
    a circuit. The Load Name is only the fallback."""
    if circuit is None:
        return None
    try:
        kind = str(circuit.CircuitType)
        if kind.endswith("Spare"):
            return "spare"
        if kind.endswith("Space"):
            return "space"
        if kind.endswith("Circuit"):
            return None
    except Exception:
        pass
    try:
        load_name = (circuit.LoadName or "").strip().upper()
    except Exception:
        return None
    if load_name == "SPARE":
        return "spare"
    if load_name == "SPACE":
        return "space"
    return None


def read_slots(psv, n_slots):
    """[(slot, row, col, circuit, kind)] for every way the schedule shows."""
    out = []
    for slot in range(1, n_slots + 1):
        try:
            # Two 'out' parameters: IronPython returns (rows, cols), while
            # pythonnet / CPython returns (None, rows, cols). Take the last two.
            result = psv.GetCellsBySlotNumber(slot)
            row_arr, col_arr = result[-2], result[-1]
        except Exception:
            continue
        if not row_arr or len(row_arr) == 0:
            continue
        r, c = row_arr[0], col_arr[0]
        try:
            circuit = psv.GetCircuitByCell(r, c)
        except Exception:
            circuit = None
        out.append((slot, r, c, circuit, placeholder_kind(circuit)))
    return out


def slot_stats(psv, n_slots):
    """How the ways of a panel are used.

    connected / spare_slots / space_slots / free count WAYS (a 3-pole spare is
    3 spare_slots); spares / spaces count the placeholder circuits themselves.
    last_real = highest way holding a real circuit, last_any = highest way
    holding anything -- the panel cannot be made smaller than that."""
    s = {"connected": 0, "spare_slots": 0, "space_slots": 0, "free": 0,
         "spares": 0, "spaces": 0, "last_real": 0, "last_any": 0}
    seen = set()
    for slot, r, c, circuit, kind in read_slots(psv, n_slots):
        if circuit is None:
            s["free"] += 1
            continue
        s["last_any"] = max(s["last_any"], slot)
        if kind is None:
            s["connected"] += 1
            s["last_real"] = max(s["last_real"], slot)
            continue
        s[kind + "_slots"] += 1
        try:
            cid = eid_value(circuit.Id)
        except Exception:
            cid = (r, c)
        if cid not in seen:
            seen.add(cid)
            s[kind + "s"] += 1
    return s


def count_connected_ways(psv, total_capacity):
    return slot_stats(psv, total_capacity)["connected"]


def get_capacity_param(panel_element):
    """The panel's number-of-ways parameter. A WRITABLE one is preferred:
    a panel can expose a read-only built-in (e.g. the pole-breaker count on
    a switchboard) while the parameter that actually drives it is another
    one, so stopping at the first one found blocked the resize."""
    if panel_element is None:
        return None
    found = []
    for bname in CAPACITY_BUILTINS:
        bip = builtin(bname)
        if bip is None:
            continue
        try:
            p = panel_element.get_Parameter(bip)
        except Exception:
            p = None
        if p is not None:
            found.append(p)
    for pname in CAPACITY_PARAM_NAMES:
        try:
            p = panel_element.LookupParameter(pname)
        except Exception:
            p = None
        if p is not None:
            found.append(p)
    for p in found:
        if not p.IsReadOnly:
            return p
    return found[0] if found else None


def panel_name_of(panel_element, psv):
    """The PANEL NAME (e.g. LP-01). Element.Name of a panel is its family
    type name, which is not what the type table should be matched against."""
    name = param_text(panel_element, "RBS_ELEC_PANEL_NAME")
    if name:
        return name
    try:
        return psv.Name or ""
    except Exception:
        return ""


class SilentFailuresPreprocessor(DB.IFailuresPreprocessor):
    """Prevents Revit's own failure/warning dialogs from popping up and
    blocking the script. Warnings are dismissed; an error rolls the panel's
    transaction back and its text is kept so it can be shown to the user."""

    def __init__(self):
        self.errors = []

    def PreprocessFailures(self, failures_accessor):
        failures = list(failures_accessor.GetFailureMessages())
        if not failures:
            return DB.FailureProcessingResult.Continue

        blocking = False
        for f in failures:
            if f.GetSeverity() == DB.FailureSeverity.Warning:
                try:
                    failures_accessor.DeleteWarning(f)
                except Exception:
                    pass
            else:
                blocking = True
                try:
                    self.errors.append(f.GetDescriptionText())
                except Exception:
                    self.errors.append("Revit error")

        if blocking:
            try:
                failures_accessor.RollBackPendingTransaction()
            except Exception:
                pass
            return DB.FailureProcessingResult.ProceedWithRollBack
        return DB.FailureProcessingResult.Continue


# ---------------------------------------------------------------------------
# Rules: panel type + standard sizes
# ---------------------------------------------------------------------------
def get_panel_type_config(panel_name, type_rows):
    name = (panel_name or "").upper()
    candidates = sorted([r for r in type_rows if str(r.Prefix or "").strip()],
                        key=lambda r: len(str(r.Prefix)), reverse=True)
    for row in candidates:
        prefix = str(row.Prefix).strip().upper()
        if name.startswith(prefix):
            try:
                return str(row.Prefix).strip(), int(float(row.SpareRating)), int(float(row.Poles))
            except Exception:
                return str(row.Prefix).strip(), DEFAULT_FALLBACK_RATING, DEFAULT_FALLBACK_POLES
    return OTHER_TYPE, DEFAULT_FALLBACK_RATING, DEFAULT_FALLBACK_POLES


def ways_table(ways_rows):
    """[(ways, spare, space)] sorted, rows that are not numbers left out."""
    out = []
    for row in ways_rows:
        try:
            ways = int(float(row.Ways))
            if ways > 0:
                out.append((ways, int(float(row.Spare)), int(float(row.Space))))
        except Exception:
            continue
    return sorted(out)


def get_spare_space_counts(ways_rows, total_ways):
    """Panels come in standard sizes (12, 18, 24, 30, 36, 42, 48, 54-way).
    Given the connected-circuit count, round UP to the smallest standard size
    that holds it and use THAT size's Spare/Space quota. E.g. 29 connected
    circuits use the 30-way row (3 spare / 3 space), not 10% of 29."""
    for ways, spare, space in ways_table(ways_rows):
        if ways >= total_ways:
            return spare, space
    # Bigger than every listed size: a straight 10%.
    return int(ceil(total_ways * 0.10)), int(ceil(total_ways * 0.10))


def candidate_sizes(ways_rows, needed):
    """Standard sizes that can hold `needed` ways, smallest first."""
    sizes = sorted(set(w for w, _, _ in ways_table(ways_rows) if w >= needed))
    if not sizes:
        sizes = [needed + (needed % 2)]
    return sizes


def ways_needed(stats, spare_needed, space_needed, poles, remove_existing):
    """Ways the panel must have for its circuits plus the requested
    placeholders -- never fewer than the highest way already in use."""
    if remove_existing:
        add_spare, add_space = spare_needed, space_needed
        used = stats["connected"]
        last = stats["last_real"]
    else:
        add_spare = max(0, spare_needed - stats["spares"])
        add_space = max(0, space_needed - stats["spaces"])
        used = stats["connected"] + stats["spare_slots"] + stats["space_slots"]
        last = stats["last_any"]
    return max(used + (add_spare + add_space) * poles, last), add_spare, add_space


def predict_ways(row, ways_rows, options):
    """Fill row.WaysAfter (what Step 2 shows before anything is changed)."""
    try:
        spare = to_int(row.SpareNeeded, "Spare Needed")
        space = to_int(row.SpaceNeeded, "Space Needed")
        poles = to_int(row.Poles, "Poles", 1, 3)
    except ValueError:
        row.WaysAfter = "?"
        return
    if not options.get("resize", True):
        row.WaysAfter = row.WaysNow
        return
    needed, _, _ = ways_needed(row._stats, spare, space, poles,
                               options.get("remove_existing", True))
    row.WaysAfter = candidate_sizes(ways_rows, needed)[0]


# ---------------------------------------------------------------------------
# Filtering the Step 1 list (no WPF in here, so it can be tested on its own)
# ---------------------------------------------------------------------------
def row_matches(row, search="", type_name=ALL, level=ALL, system=ALL, show=SHOW_ALL):
    if type_name not in (None, "", ALL) and row.Type != type_name:
        return False
    if level not in (None, "", ALL) and row.Level != level:
        return False
    if system not in (None, "", ALL) and row.System != system:
        return False
    if show == SHOW_TICKED and not row.Selected:
        return False
    if show == SHOW_UNTICKED and row.Selected:
        return False
    if show == SHOW_FREE and not row.Free:
        return False
    if show == SHOW_NO_PLACEHOLDER and (row.Spares or row.Spaces):
        return False
    if show == SHOW_REVIT and not row.IsPicked:
        return False
    terms = (search or "").upper().split()
    if terms:
        text = u" ".join(unicode(v) for v in (row.Name, row.Type, row.Level,
                                              row.System, row.SupplyFrom)).upper()
        for term in terms:
            if term not in text:
                return False
    return True


# ---------------------------------------------------------------------------
# Changing a panel schedule
# ---------------------------------------------------------------------------
def remove_existing_spares_and_spaces(psv, total_slots, panel_name, log_fn):
    """Remove every Spare/Space circuit from a panel schedule.

    A multi-pole placeholder appears in more than one way, so its ElementId
    is used to remove it only once. Returns (spares removed, spaces removed)."""
    placeholders = {}
    for slot, row, col, circuit, kind in read_slots(psv, total_slots):
        if kind is None:
            continue
        circuit_id = eid_value(circuit.Id)
        if circuit_id not in placeholders:
            placeholders[circuit_id] = (kind, row, col)

    removed_spares, removed_spaces = 0, 0
    # Bottom-up, so a removal cannot shift a row that is still to be removed.
    entries = sorted(placeholders.values(), key=lambda item: (item[1], item[2]),
                     reverse=True)
    for kind, row, col in entries:
        try:
            if kind == "spare":
                psv.RemoveSpare(row, col)
                removed_spares += 1
            else:
                psv.RemoveSpace(row, col)
                removed_spaces += 1
        except Exception as ex:
            log_fn("  [WARN] {}: could not remove existing {} at row {} "
                   "col {} ({})".format(panel_name, kind, row, col, ex))

    if removed_spares or removed_spaces:
        doc.Regenerate()
        log_fn("  {}: removed {} existing spare(s) and {} existing space(s) "
               "before rebuilding the layout.".format(
                   panel_name, removed_spares, removed_spaces))
    return removed_spares, removed_spaces


def set_spare_rating(circuit, amps):
    """Write the breaker rating of a spare. The rating is a decimal (Double)
    parameter: handing it a whole number is silently ignored by Revit."""
    p = circuit.get_Parameter(DB.BuiltInParameter.RBS_ELEC_CIRCUIT_RATING_PARAM)
    if p is None or p.IsReadOnly:
        return False
    value = float(amps)
    try:
        value = DB.UnitUtils.ConvertToInternalUnits(value, DB.UnitTypeId.Amperes)
    except Exception:
        pass
    return bool(p.Set(value))


def fill_one_panel(psv, panel_name, spare_needed, space_needed,
                   spare_rating, poles, ways_rows, log_fn, options=None):
    """Give a panel schedule the requested Spare/Space layout.

    Returns a dict: spare_placed, space_placed, spare_wanted, space_wanted,
    extra_space, ways_before, ways_after, warnings."""
    opts = dict(DEFAULT_OPTIONS)
    opts.update(options or {})
    warnings = [0]

    def warn(message):
        warnings[0] += 1
        log_fn("  [WARN] {}: {}".format(panel_name, message))

    total_slots = psv.GetTableData().NumberOfSlots
    ways_before = total_slots
    if opts["remove_existing"]:
        remove_existing_spares_and_spaces(psv, total_slots, panel_name, log_fn)
        total_slots = psv.GetTableData().NumberOfSlots
    stats = slot_stats(psv, total_slots)
    raw_target, add_spare, add_space = ways_needed(
        stats, spare_needed, space_needed, poles, opts["remove_existing"])
    if not opts["remove_existing"] and (stats["spares"] or stats["spaces"]):
        log_fn("  {}: keeps its {} spare(s) and {} space(s); adding {} spare(s) "
               "and {} space(s).".format(panel_name, stats["spares"], stats["spaces"],
                                         add_spare, add_space))

    # Standard sizes that fit, smallest first: when the smallest one has not
    # enough CONTIGUOUS room for a multi-pole item, the next size is tried.
    if opts["resize"]:
        candidates = candidate_sizes(ways_rows, raw_target)
    else:
        candidates = [total_slots]

    def set_capacity_to(new_capacity, current_slots):
        if new_capacity == current_slots:
            return current_slots
        panel_el = doc.GetElement(psv.GetPanel())
        cap_param = get_capacity_param(panel_el)
        if cap_param is None or cap_param.IsReadOnly:
            warn("could not change the number of ways from {} to {} because the "
                 "panel's breaker-count parameter is missing or read-only. Set "
                 "it in the panel's properties, then run again.".format(
                     current_slots, new_capacity))
            return current_slots
        try:
            cap_param.Set(int(new_capacity))
            doc.Regenerate()
        except Exception as ex:
            warn("could not change the number of ways from {} to {} ({}).".format(
                current_slots, new_capacity, ex))
            return current_slots
        new_total_slots = psv.GetTableData().NumberOfSlots
        if new_total_slots != new_capacity:
            warn("the number of ways was set to {}, but the SCHEDULE shows {}. "
                 "Its panel schedule TEMPLATE probably has 'Number of slots "
                 "shown' set to a fixed value -- change it to 'Variable based "
                 "on max number of single pole breakers', then run again.".format(
                     new_capacity, new_total_slots))
        else:
            log_fn("  {}: number of ways {} from {} to {}.".format(
                panel_name, "increased" if new_total_slots > current_slots else "reduced",
                current_slots, new_total_slots))
        return new_total_slots

    def build_maps(n_slots):
        """(row, col) of every way and whether it is occupied -- a multi-pole
        item needs consecutive free ROWS in the SAME COLUMN, which is not the
        same thing as consecutive way numbers."""
        all_cells = []
        occupied_by_rc = {}
        for slot, r, c, circuit, kind in read_slots(psv, n_slots):
            if (r, c) in occupied_by_rc:
                continue
            all_cells.append((r, c))
            occupied_by_rc[(r, c)] = circuit is not None
        all_cells.sort(key=lambda rc: (rc[1], rc[0]))
        rows = [r for r, c in all_cells]
        return all_cells, occupied_by_rc, (min(rows) if rows else 0)

    def find_free_run(all_cells, occupied_by_rc, base_row, n_poles, start_idx,
                      require_phase_align=False):
        for idx in range(start_idx, len(all_cells)):
            row, col = all_cells[idx]
            if require_phase_align and ((row - base_row) % 3 != 0):
                continue
            if all(not occupied_by_rc.get((row + k, col), True) for k in range(n_poles)):
                return idx
        return None

    rating_failed = [False]

    def place_one(all_cells, occupied_by_rc, base_row, kind, n_poles, start_idx):
        phase_aligned = False
        idx = None
        if n_poles == 3:
            idx = find_free_run(all_cells, occupied_by_rc, base_row,
                                n_poles, start_idx, require_phase_align=True)
            phase_aligned = idx is not None
        if idx is None:
            idx = find_free_run(all_cells, occupied_by_rc, base_row,
                                n_poles, start_idx, require_phase_align=False)
        if idx is None:
            return None
        r, c = all_cells[idx]
        actual_poles = 1

        sub = DB.SubTransaction(doc)
        sub.Start()
        try:
            if kind == "spare":
                psv.AddSpare(r, c)
            else:
                psv.AddSpace(r, c)

            circuit = psv.GetCircuitByCell(r, c)
            if circuit is not None:
                if n_poles > 1:
                    poles_param = circuit.get_Parameter(
                        DB.BuiltInParameter.RBS_ELEC_NUMBER_OF_POLES)
                    if poles_param and not poles_param.IsReadOnly:
                        poles_param.Set(int(n_poles))
                        doc.Regenerate()
                    try:
                        actual_poles = int(circuit.PolesNumber)
                    except Exception:
                        actual_poles = n_poles
                if kind == "spare" and not set_spare_rating(circuit, spare_rating):
                    rating_failed[0] = True
            status = sub.Commit()
        except Exception as ex:
            if not sub.HasEnded():
                sub.RollBack()
            warn("failed to add {} at row {} col {} ({})".format(kind, r, c, ex))
            return ("retry", idx + 1)

        if status != DB.TransactionStatus.Committed:
            warn("Revit rejected {} at row {} col {} -- trying the next way.".format(kind, r, c))
            return ("retry", idx + 1)

        for k in range(max(1, actual_poles)):
            occupied_by_rc[(r + k, c)] = True
        if n_poles > 1 and actual_poles != n_poles:
            warn("the {} at row {} col {} stayed {}-pole: Revit did not accept "
                 "{} poles there.".format(kind, r, c, actual_poles, n_poles))
        elif n_poles == 3 and not phase_aligned:
            log_fn("  [NOTE] {}: a 3-pole {} at row {} col {} does not start on "
                   "the first phase -- check its phase order.".format(panel_name, kind, r, c))
        return idx + 1

    def place_remaining(all_cells, occupied_by_rc, base_row, kind, n_poles,
                        needed, already_placed):
        placed = already_placed
        cursor = 0
        while placed < needed:
            result = place_one(all_cells, occupied_by_rc, base_row, kind, n_poles, cursor)
            if result is None:
                break
            if isinstance(result, tuple):
                # Skip PAST the way that was refused. (Advancing the cursor
                # by only 1 from where the search started found the same
                # refused way again and again, logging a warning each time.)
                cursor = result[1]
                continue
            cursor = result
            placed += 1
        return placed

    spare_placed, space_placed = 0, 0
    for idx, candidate_capacity in enumerate(candidates):
        total_slots = set_capacity_to(candidate_capacity, total_slots)
        # The size could not be changed (read-only parameter, or the schedule
        # template fixes the slot count): trying the next standard size would
        # fail the same way and repeat the same warning.
        resize_blocked = total_slots != candidate_capacity
        all_cells, occupied_by_rc, base_row = build_maps(total_slots)

        spare_placed = place_remaining(all_cells, occupied_by_rc, base_row,
                                       "spare", poles, add_spare, spare_placed)
        space_placed = place_remaining(all_cells, occupied_by_rc, base_row,
                                       "space", poles, add_space, space_placed)
        if spare_placed >= add_spare and space_placed >= add_space:
            break
        if resize_blocked:
            break
        if idx < len(candidates) - 1:
            log_fn("  {}: {} ways has not enough room next to each other -- "
                   "trying the next standard size ({}).".format(
                       panel_name, candidate_capacity, candidates[idx + 1]))

    if spare_placed < add_spare or space_placed < add_space:
        warn("only {} of {} spare(s) and {} of {} space(s) fit in {} ways{}".format(
            spare_placed, add_spare, space_placed, add_space, total_slots,
            "." if opts["resize"] else " ('Resize the panel' is off)."))
    if rating_failed[0]:
        warn("the spare breaker rating could not be written (the Rating "
             "parameter of the spare is read-only or refused {} A).".format(spare_rating))

    extra_space_placed = 0
    if opts["fill_leftover"]:
        all_cells, occupied_by_rc, base_row = build_maps(total_slots)
        extra_space_placed = place_remaining(all_cells, occupied_by_rc, base_row,
                                             "space", 1, len(all_cells), 0)
        if extra_space_placed:
            log_fn("  {}: also marked {} leftover empty way(s) as Space.".format(
                panel_name, extra_space_placed))

    return {"spare_placed": spare_placed, "space_placed": space_placed,
            "spare_wanted": add_spare, "space_wanted": add_space,
            "extra_space": extra_space_placed, "ways_before": ways_before,
            "ways_after": total_slots, "warnings": warnings[0]}


# ---------------------------------------------------------------------------
# STEP 2 window
# ---------------------------------------------------------------------------
class ConfigWindow(forms.WPFWindow):
    def __init__(self, xaml_file, picked_rows, ways_rows, type_rows, options=None):
        forms.WPFWindow.__init__(self, themed_xaml(xaml_file), literal_string=True)
        self.go_back = False
        self.changed = False
        self.ways_rows = list(ways_rows)
        self._ready = False

        opts = dict(DEFAULT_OPTIONS)
        opts.update(options or {})
        self.chkRemove.IsChecked = bool(opts["remove_existing"])
        self.chkResize.IsChecked = bool(opts["resize"])
        self.chkLeftover.IsChecked = bool(opts["fill_leftover"])

        self.config_rows = ObservableCollection[object]()
        for pr in picked_rows:
            load_row_stats(pr)
            spare_needed, space_needed = get_spare_space_counts(ways_rows, pr.Connected)
            _, rating, poles = get_panel_type_config(pr.Name, type_rows)
            self.config_rows.Add(PanelConfigRow(pr, spare_needed, space_needed, rating, poles))
        self.dgConfig.ItemsSource = self.config_rows
        self.dgConfig.AddHandler(ButtonBase.ClickEvent, RoutedEventHandler(self.include_clicked))
        self._ready = True
        self.recalculate()

        self.log("Loaded {} panel(s). Edit Spare / Space Needed, Spare Rating or "
                 "Poles for any panel, then click 'Fill / Update'.".format(
                     self.config_rows.Count))

    # ---- helpers
    def options(self):
        return {"remove_existing": bool(self.chkRemove.IsChecked),
                "resize": bool(self.chkResize.IsChecked),
                "fill_leftover": bool(self.chkLeftover.IsChecked)}

    def log(self, msg):
        self.txtLog.AppendText(msg + "\n")
        self.txtLog.ScrollToEnd()

    def set_busy(self, busy):
        for name in ("btnBack", "btnFill", "btnClose", "btnBulk", "btnRecalc"):
            btn = getattr(self, name, None)
            if btn is not None:
                btn.IsEnabled = not busy

    def recalculate(self):
        """Refresh 'Ways after' and the totals line from what is in the grid."""
        commit_edits(self.dgConfig)
        opts = self.options()
        panels = spares = spaces = 0
        for row in self.config_rows:
            predict_ways(row, self.ways_rows, opts)
            if row.Include:
                panels += 1
                try:
                    spares += to_int(row.SpareNeeded)
                    spaces += to_int(row.SpaceNeeded)
                except ValueError:
                    pass
        refresh_grid(self.dgConfig)
        self.tilePanels.Text = str(panels)
        self.tileSpares.Text = str(spares)
        self.tileSpaces.Text = str(spaces)

    # ---- events
    def include_clicked(self, sender, args):
        if self._ready and args.OriginalSource.GetType().Name == "CheckBox":
            self.recalculate()

    def option_changed(self, sender, args):
        if getattr(self, "_ready", False):
            self.recalculate()

    def recalc_click(self, sender, args):
        self.recalculate()

    def bulk_click(self, sender, args):
        """Write the filled-in boxes into the highlighted rows (all rows when
        none is highlighted). A box left empty is not changed."""
        commit_edits(self.dgConfig)
        try:
            values = {}
            for attr, box, label, lo, hi in (
                    ("SpareNeeded", self.txtBulkSpare.Text, "Spare", 0, None),
                    ("SpaceNeeded", self.txtBulkSpace.Text, "Space", 0, None),
                    ("SpareRating", self.cbBulkRating.Text, "Rating", 1, None),
                    ("Poles", self.cbBulkPoles.Text, "Poles", 1, 3)):
                if (box or "").strip():
                    values[attr] = to_int(box, label, lo, hi)
        except ValueError as ex:
            forms.alert(str(ex), title="Check the values")
            return
        if not values:
            forms.alert("Type a value in at least one of the boxes first.",
                        title="Nothing to apply")
            return
        targets = list(self.dgConfig.SelectedItems) or list(self.config_rows)
        for row in targets:
            for attr, value in values.items():
                setattr(row, attr, value)
        self.recalculate()
        self.log("Applied {} to {} panel(s).".format(
            ", ".join("{} = {}".format(k, v) for k, v in sorted(values.items())), len(targets)))

    def back_click(self, sender, args):
        self.go_back = True
        self.Close()

    def close_click(self, sender, args):
        self.Close()

    def fill_click(self, sender, args):
        commit_edits(self.dgConfig)
        included = [r for r in self.config_rows if r.Include]
        if not included:
            forms.alert("Nothing is included. Tick at least one panel.",
                        title="Nothing to update")
            return

        # Check every value BEFORE touching the model.
        jobs = []
        for row in included:
            try:
                jobs.append((row,
                             to_int(row.SpareNeeded, "Spare Needed"),
                             to_int(row.SpaceNeeded, "Space Needed"),
                             to_int(row.SpareRating, "Spare Rating", 1),
                             to_int(row.Poles, "Poles", 1, 3)))
            except ValueError as ex:
                forms.alert("{}: {}.\n\nNothing was changed.".format(row.Name, ex),
                            title="Check the values")
                return

        opts = self.options()
        if not forms.alert(
                "This will change {} panel schedule(s):\n\n"
                "  - {}\n  - {}\n  - {}\n\n"
                "Recommended: try it on a copy of the model first.\n\nContinue?".format(
                    len(included),
                    "existing Spare / Space are REMOVED and rebuilt" if opts["remove_existing"]
                    else "existing Spare / Space are kept, only the shortfall is added",
                    "the number of ways is set to the standard size" if opts["resize"]
                    else "the number of ways is not changed",
                    "every way still empty becomes a Space" if opts["fill_leftover"]
                    else "ways still empty stay empty"),
                title="Confirm", yes=True, no=True):
            return

        self.set_busy(True)
        self.pbar.Maximum = len(jobs)
        self.pbar.Value = 0
        self.pbar.Visibility = Visibility.Visible
        ok = partial = fail = 0
        group = DB.TransactionGroup(doc, "Fill Spare/Space Circuits")
        group.Start()
        try:
            for i, (row, spare, space, rating, poles) in enumerate(jobs):
                outcome = self._fill_row(row, spare, space, rating, poles, opts)
                if outcome == "ok":
                    ok += 1
                elif outcome == "warn":
                    partial += 1
                else:
                    fail += 1
                self.pbar.Value = i + 1
                refresh_grid(self.dgConfig)
                pump_ui()
            group.Assimilate()
        except Exception as ex:
            try:
                group.RollBack()
            except Exception:
                pass
            for row, _, _, _, _ in jobs:
                row.Status, row.StatusKind = "Rolled back", "error"
            self.log("Everything was rolled back: {}".format(ex))
            logger.debug(traceback.format_exc())
            forms.alert("The run failed and was rolled back:\n{}".format(ex), title="Error")
            ok = partial = 0
            fail = len(jobs)

        self.changed = self.changed or bool(ok or partial)
        self.pbar.Visibility = Visibility.Collapsed
        self.set_busy(False)
        refresh_grid(self.dgConfig)
        self.log("Done. {} panel(s) updated, {} updated with warnings, {} failed.".format(
            ok, partial, fail))

    def _fill_row(self, row, spare, space, rating, poles, opts):
        """One panel = one transaction. Returns 'ok', 'warn' or 'error'."""
        psv = doc.GetElement(DB.ElementId(row._element_id))
        if psv is None:
            row.Status, row.StatusKind = "Panel schedule no longer exists", "error"
            return "error"
        preprocessor = SilentFailuresPreprocessor()
        t = DB.Transaction(doc, "Fill Spare/Space - {}".format(row.Name))
        t.Start()
        fho = t.GetFailureHandlingOptions()
        fho.SetFailuresPreprocessor(preprocessor)
        fho.SetForcedModalHandling(False)
        t.SetFailureHandlingOptions(fho)
        try:
            result = fill_one_panel(psv, row.Name, spare, space, rating, poles,
                                    self.ways_rows, self.log, opts)
            status = t.Commit()
        except Exception as ex:
            try:
                if t.HasStarted() and not t.HasEnded():
                    t.RollBack()
            except Exception:
                pass
            row.Status, row.StatusKind = "Error: {}".format(ex), "error"
            self.log("  [ERROR] {}: {}".format(row.Name, ex))
            logger.debug(traceback.format_exc())
            return "error"

        # Commit() does not raise when Revit rolls the transaction back, it
        # only says so in its return value.
        if status != DB.TransactionStatus.Committed:
            reason = "; ".join(preprocessor.errors) or "no reason given"
            row.Status, row.StatusKind = "Revit rolled it back: {}".format(reason), "error"
            self.log("  [ERROR] {}: Revit rolled the change back ({}). "
                     "Nothing was changed in this panel.".format(row.Name, reason))
            return "error"

        short = (result["spare_placed"] < result["spare_wanted"]
                 or result["space_placed"] < result["space_wanted"])
        text = "{} spare, {} space".format(
            result["spare_placed"], result["space_placed"] + result["extra_space"])
        if result["ways_after"] != result["ways_before"]:
            text += "  |  {} -> {} ways".format(result["ways_before"], result["ways_after"])
        row.WaysNow = row.WaysAfter = result["ways_after"]
        kind = "warn" if (short or result["warnings"]) else "ok"
        row.Status = ("Check log: " if kind == "warn" else "Done: ") + text
        row.StatusKind = kind
        self.log("{}: placed {}/{} spare, {}/{} space{}.".format(
            row.Name, result["spare_placed"], result["spare_wanted"],
            result["space_placed"], result["space_wanted"],
            " (+{} leftover space)".format(result["extra_space"]) if result["extra_space"] else ""))
        try:
            row._stats = slot_stats(psv, result["ways_after"])
            row.ConnectedWays = row._stats["connected"]
        except Exception:
            pass
        return kind


# ---------------------------------------------------------------------------
# STEP 1 window
# ---------------------------------------------------------------------------
class SelectWindow(forms.WPFWindow):
    def __init__(self, xaml_file, initial_ways=None, initial_types=None,
                 ticked_ids=None):
        forms.WPFWindow.__init__(self, xaml_file)
        self.proceed = False
        self._ready = False

        self.ways_rows = ObservableCollection[object]()
        for ways, spare, space in (initial_ways or DEFAULT_WAYS_TABLE):
            self.ways_rows.Add(WaysRow(ways, spare, space))
        self.dgWaysTable.ItemsSource = self.ways_rows

        self.type_rows = ObservableCollection[object]()
        for prefix, rating, poles in (initial_types or DEFAULT_TYPE_CONFIG):
            self.type_rows.Add(TypeConfigRow(prefix, rating, poles))
        self.dgTypeConfig.ItemsSource = self.type_rows

        # panel_rows is the complete list; the grid shows visible_panel_rows,
        # so filtering never loses a tick.
        self.panel_rows = []
        self.visible_panel_rows = ObservableCollection[object]()
        self.dgPanels.ItemsSource = self.visible_panel_rows
        self.dgPanels.AddHandler(ButtonBase.ClickEvent, RoutedEventHandler(self.tick_clicked))

        for name in SHOW_CHOICES:
            self.cbShow.Items.Add(name)
        self.cbShow.SelectedIndex = 0
        self._ready = True
        self._pending = []
        self._load_total = 0
        self._hint = self.hintText.Text

        # Fast part now (names, levels, sizes); the ways of each panel are
        # read in small batches once the window is on screen.
        self.scan(ticked_ids)
        self.Loaded += self._on_loaded
        self.Closed += self._on_closed

    # ---- helpers
    def log(self, msg):
        self.txtLog.AppendText(msg + "\n")
        self.txtLog.ScrollToEnd()

    def set_busy(self, busy):
        for name in ("btnScan", "btnUseSelection", "btnTickShown", "btnUntickShown",
                     "btnInvert", "btnNext", "btnClose", "btnClearFilters"):
            btn = getattr(self, name, None)
            if btn is not None:
                btn.IsEnabled = not busy

    def table_values(self):
        commit_edits(self.dgWaysTable)
        commit_edits(self.dgTypeConfig)
        ways = ways_table(self.ways_rows)
        types = []
        for r in self.type_rows:
            prefix = str(r.Prefix or "").strip()
            if not prefix:
                continue
            try:
                types.append((prefix, int(float(r.SpareRating)), int(float(r.Poles))))
            except Exception:
                types.append((prefix, DEFAULT_FALLBACK_RATING, DEFAULT_FALLBACK_POLES))
        return ways, types

    def selected_rows(self):
        return [r for r in self.panel_rows if r.Selected]

    # ---- reading the ways in the background
    def _on_loaded(self, sender, args):
        self.start_loading()

    def _on_closed(self, sender, args):
        self._pending = []

    def start_loading(self):
        """Queue the rows whose ways have not been read; ticked rows first."""
        self._pending = sorted([r for r in self.panel_rows if r._stats is None],
                               key=lambda r: not r.Selected)
        self._load_total = len(self._pending)
        if self._pending:
            self._queue_batch()

    def _queue_batch(self):
        Dispatcher.CurrentDispatcher.BeginInvoke(
            DispatcherPriority.Background, System.Action(self._load_batch))

    def _load_batch(self):
        """Read panels for about a tenth of a second, then let the window
        breathe, so it can be used while the list fills in."""
        if not self._pending:
            return
        started = time.time()
        while self._pending and time.time() - started < 0.1:
            load_row_stats(self._pending.pop(0))
        refresh_grid(self.dgPanels)
        if self._pending:
            self.hintText.Text = "Reading panel ways: {} of {} ...   (you can already filter and tick)".format(
                self._load_total - len(self._pending), self._load_total)
            self._queue_batch()
        else:
            self.hintText.Text = self._hint
            self.apply_filters()

    def load_all_stats(self, rows=None):
        """Finish reading now (needed by the filters that look at the ways)."""
        todo = [r for r in (rows if rows is not None else self.panel_rows) if r._stats is None]
        for i, row in enumerate(todo):
            load_row_stats(row)
            if (i + 1) % 25 == 0:
                self.hintText.Text = "Reading panel ways: {} of {} ...".format(i + 1, len(todo))
                pump_ui()
        if rows is None:
            self._pending = []
            self.hintText.Text = self._hint
        if todo:
            refresh_grid(self.dgPanels)

    # ---- scanning
    def panel_ids_in_revit_selection(self):
        """Ids of the panel schedules that belong to what is selected in
        Revit: a schedule in the Project Browser, a schedule placed on a
        sheet, or the panel itself."""
        try:
            selected = list(uidoc.Selection.GetElementIds())
        except Exception:
            return set()
        if not selected:
            return set()
        by_panel = {}
        for psv in DB.FilteredElementCollector(doc).OfClass(DB.Electrical.PanelScheduleView):
            try:
                by_panel[eid_value(psv.GetPanel())] = eid_value(psv.Id)
            except Exception:
                continue
        schedule_ids = set(by_panel.values())
        found = set()
        for eid in selected:
            value = eid_value(eid)
            if value in schedule_ids:
                found.add(value)
                continue
            if value in by_panel:
                found.add(by_panel[value])
                continue
            el = doc.GetElement(eid)
            sid = getattr(el, "ScheduleId", None)      # schedule graphic on a sheet
            if sid is not None:
                try:
                    if eid_value(sid) in schedule_ids:
                        found.add(eid_value(sid))
                except Exception:
                    pass
        return found

    def scan(self, ticked_ids=None):
        """Read every panel schedule of the project. Ticks are kept."""
        self.set_busy(True)
        try:
            keep = set(ticked_ids) if ticked_ids is not None else \
                set(r._element_id for r in self.selected_rows())
            picked = self.panel_ids_in_revit_selection()
            first_time = ticked_ids is None and not self.panel_rows
            _, types = self.table_values()
            type_rows = [TypeConfigRow(*t) for t in types]

            rows = []
            failed = 0
            for psv in DB.FilteredElementCollector(doc).OfClass(DB.Electrical.PanelScheduleView):
                try:
                    if getattr(psv, "IsTemplate", False):
                        continue
                    panel_el = doc.GetElement(psv.GetPanel())
                    name = panel_name_of(panel_el, psv)
                    prefix, _, _ = get_panel_type_config(name, type_rows)
                    ways = psv.GetTableData().NumberOfSlots
                    level = ""
                    try:
                        level_el = doc.GetElement(panel_el.LevelId)
                        level = level_el.Name if level_el is not None else ""
                    except Exception:
                        pass
                    row = PanelPickRow(
                        eid_value(psv.Id), name, prefix, level,
                        param_text(panel_el, "RBS_FAMILY_CONTENT_DISTRIBUTION_SYSTEM"),
                        param_text(panel_el, "RBS_ELEC_PANEL_SUPPLY_FROM_PARAM"),
                        ways, None)
                except Exception as ex:
                    failed += 1
                    logger.debug(traceback.format_exc())
                    self.log("  [WARN] a panel schedule could not be read ({}).".format(ex))
                    continue
                row.IsPicked = row._element_id in picked
                row.Selected = row._element_id in keep or (first_time and row.IsPicked)
                rows.append(row)
                if len(rows) % 40 == 0:
                    pump_ui()

            rows.sort(key=lambda r: (r.Name or "").upper())
            self.panel_rows = rows
            self.fill_filter_lists()
            self.apply_filters()
            if self.IsLoaded:            # a Rescan: the window is already up
                self.start_loading()
            self.log("Found {} panel schedule(s){}.".format(
                len(rows), ", {} could not be read".format(failed) if failed else ""))
            if picked:
                self.log("{} of them belong to the current Revit selection "
                         "(yellow rows{}).".format(
                             len([r for r in rows if r.IsPicked]),
                             ", already ticked" if first_time else ""))
            if not rows and not failed:
                self.log("No panel schedules in this project. Create them first "
                         "(select a panel > Create Panel Schedules).")
        finally:
            self.set_busy(False)

    # ---- filters
    def fill_filter_lists(self):
        self._ready = False
        try:
            for combo, attr in ((self.cbType, "Type"), (self.cbLevel, "Level"),
                                (self.cbSystem, "System")):
                current = combo.SelectedItem
                values = sorted(set(getattr(r, attr) for r in self.panel_rows if getattr(r, attr)))
                combo.Items.Clear()
                combo.Items.Add(ALL)
                for v in values:
                    combo.Items.Add(v)
                combo.SelectedItem = current if current in values else ALL
        finally:
            self._ready = True

    def apply_filters(self):
        if not getattr(self, "_ready", False):
            return
        search = self.txtPanelSearch.Text or ""
        self.searchHint.Visibility = Visibility.Collapsed if search else Visibility.Visible
        if self.cbShow.SelectedItem in (SHOW_FREE, SHOW_NO_PLACEHOLDER):
            self.load_all_stats()        # these two look at the ways
        args = (search, self.cbType.SelectedItem, self.cbLevel.SelectedItem,
                self.cbSystem.SelectedItem, self.cbShow.SelectedItem)
        self.visible_panel_rows.Clear()
        for row in self.panel_rows:
            if row_matches(row, *args):
                self.visible_panel_rows.Add(row)
        self.update_counts()

    def update_counts(self):
        self.tileTotal.Text = str(len(self.panel_rows))
        self.tileShown.Text = str(self.visible_panel_rows.Count)
        ticked = len(self.selected_rows())
        self.tileTicked.Text = str(ticked)
        self.btnNext.Content = "Next: configure {} panel(s)  >".format(ticked) if ticked \
            else "Next: configure  >"

    def filter_changed(self, sender, args):
        self.apply_filters()

    def clear_filters_click(self, sender, args):
        self._ready = False
        self.txtPanelSearch.Text = ""
        for combo in (self.cbType, self.cbLevel, self.cbSystem, self.cbShow):
            combo.SelectedIndex = 0
        self._ready = True
        self.apply_filters()

    # ---- ticking
    def _after_ticks(self, message=None):
        # A "Ticked only" / "Not ticked" view has to drop the rows that no
        # longer belong in it.
        if self.cbShow.SelectedItem in (SHOW_TICKED, SHOW_UNTICKED):
            self.apply_filters()
        else:
            self.update_counts()
        refresh_grid(self.dgPanels)
        if message:
            self.log(message)

    def tick_clicked(self, sender, args):
        if args.OriginalSource.GetType().Name == "CheckBox":
            self.update_counts()

    def grid_key(self, sender, args):
        """Space bar ticks / unticks every highlighted row."""
        if args.Key != Key.Space:
            return
        rows = list(self.dgPanels.SelectedItems)
        if not rows:
            return
        state = not all(r.Selected for r in rows)
        for r in rows:
            r.Selected = state
        args.Handled = True
        self._after_ticks()

    def tick_shown_click(self, sender, args):
        rows = list(self.visible_panel_rows)
        for r in rows:
            r.Selected = True
        self._after_ticks("Ticked the {} panel(s) shown.".format(len(rows)))

    def untick_shown_click(self, sender, args):
        rows = list(self.visible_panel_rows)
        for r in rows:
            r.Selected = False
        self._after_ticks("Unticked the {} panel(s) shown.".format(len(rows)))

    def invert_click(self, sender, args):
        rows = list(self.visible_panel_rows)
        for r in rows:
            r.Selected = not r.Selected
        self._after_ticks("Inverted the ticks of the {} panel(s) shown.".format(len(rows)))

    def check_highlighted_click(self, sender, args):
        rows = list(self.dgPanels.SelectedItems)
        for r in rows:
            r.Selected = True
        self._after_ticks("Ticked {} highlighted panel(s).".format(len(rows)))

    def uncheck_highlighted_click(self, sender, args):
        rows = list(self.dgPanels.SelectedItems)
        for r in rows:
            r.Selected = False
        self._after_ticks("Unticked {} highlighted panel(s).".format(len(rows)))

    def use_selection_click(self, sender, args):
        picked = self.panel_ids_in_revit_selection()
        if not picked:
            forms.alert(
                "Nothing usable is selected in Revit.\n\nSelect panels, panel "
                "schedules in the Project Browser, or panel schedules placed "
                "on a sheet, then click this button again.",
                title="Nothing selected")
            return
        matched = 0
        for row in self.panel_rows:
            row.IsPicked = row._element_id in picked
            if row.IsPicked:
                row.Selected = True
                matched += 1
        self._ready = False
        self.cbShow.SelectedItem = SHOW_REVIT
        self._ready = True
        self.apply_filters()
        refresh_grid(self.dgPanels)
        self.log("Ticked {} panel(s) from the Revit selection; the list now "
                 "shows only those ('Clear filters' shows everything again).".format(matched))

    def scan_panels_click(self, sender, args):
        self.scan()

    # ---- rule tables
    def add_ways_row_click(self, sender, args):
        commit_edits(self.dgWaysTable)
        self.ways_rows.Add(WaysRow(0, 0, 0))

    def remove_ways_row_click(self, sender, args):
        commit_edits(self.dgWaysTable)
        for row in list(self.dgWaysTable.SelectedItems):
            self.ways_rows.Remove(row)

    def add_type_row_click(self, sender, args):
        commit_edits(self.dgTypeConfig)
        self.type_rows.Add(TypeConfigRow("NEW", DEFAULT_FALLBACK_RATING, DEFAULT_FALLBACK_POLES))

    def remove_type_row_click(self, sender, args):
        commit_edits(self.dgTypeConfig)
        for row in list(self.dgTypeConfig.SelectedItems):
            self.type_rows.Remove(row)

    def reset_tables_click(self, sender, args):
        commit_edits(self.dgWaysTable)
        commit_edits(self.dgTypeConfig)
        self.ways_rows.Clear()
        for ways, spare, space in DEFAULT_WAYS_TABLE:
            self.ways_rows.Add(WaysRow(ways, spare, space))
        self.type_rows.Clear()
        for prefix, rating, poles in DEFAULT_TYPE_CONFIG:
            self.type_rows.Add(TypeConfigRow(prefix, rating, poles))
        self.log("Rule tables reset to the defaults.")

    # ---- leaving
    def next_click(self, sender, args):
        if not self.selected_rows():
            forms.alert("Tick at least one panel first.", title="Nothing ticked")
            return
        ways, types = self.table_values()
        if not ways:
            forms.alert("The Ways table has no usable row.", title="Check the rules")
            return
        self.load_all_stats(self.selected_rows())
        self.proceed = True
        self.Close()

    def close_click(self, sender, args):
        self.proceed = False
        self.Close()


# ---------------------------------------------------------------------------
# Main: loop so 'Back' in Step 2 returns to Step 1 with state preserved
# ---------------------------------------------------------------------------
def main():
    settings = load_settings()
    saved_ways = [tuple(r) for r in settings.get("ways") or [] if len(r) == 3] or None
    saved_types = [tuple(r) for r in settings.get("types") or [] if len(r) == 3] or None
    options = dict(DEFAULT_OPTIONS)
    options.update(settings.get("options") or {})
    ticked_ids = None

    while True:
        select_win = SelectWindow(SELECT_XAML, saved_ways, saved_types, ticked_ids)
        select_win.ShowDialog()

        saved_ways, saved_types = select_win.table_values()
        save_settings({"ways": saved_ways, "types": saved_types, "options": options})
        if not select_win.proceed:
            return
        selected = select_win.selected_rows()
        if not selected:
            return
        ticked_ids = [r._element_id for r in selected]

        ways_rows = [WaysRow(*r) for r in saved_ways]
        type_rows = [TypeConfigRow(*r) for r in saved_types]
        config_win = ConfigWindow(CONFIG_XAML, selected, ways_rows, type_rows, options)
        config_win.ShowDialog()
        options = config_win.options()
        save_settings({"ways": saved_ways, "types": saved_types, "options": options})

        if not config_win.go_back:
            break
        # Back: Step 1 opens again and re-reads the schedules, so the list
        # shows what the panels look like now.


if __name__ == "__main__":
    main()
