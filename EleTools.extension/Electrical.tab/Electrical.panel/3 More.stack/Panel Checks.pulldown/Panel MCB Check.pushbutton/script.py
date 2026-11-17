# -*- coding: utf-8 -*-
"""Panel MCB Check -- compare every panel's MCB Rating with its circuits.

WHAT IT DOES
------------
1. Lists every electrical panel (Electrical Equipment instance) in the
   active model in a window (PanelMcbCheck.xaml), with filters: search,
   level, panel type, status, "feeds circuits", "only ticked".
2. You tick the panels to check.  Only ticked panels are checked.
3. For each ticked panel it reads the built-in "MCB Rating" (the incomer
   rating) and the "Rating" of every circuit FED BY that panel.
   Circuits with an empty / zero / unreadable Rating are ignored.
4. The MCB Rating must be MORE THAN every circuit Rating.  The panel is
   flagged when any single circuit Rating is equal to or greater than the
   panel MCB Rating, and the circuit(s) that caused it are listed.
5. Suggests the next standard rating (32, 40, 63, 80, 100, 125 A --
   editable in the window) that is strictly above the highest circuit
   Rating on the panel.
6. "Report and close" prints the results table in the pyRevit output
   window, with clickable links.
7. OPTIONAL: "Update MCB Rating" writes the suggested value into the
   ticked panels that need it.  Nothing is changed unless you press it and
   confirm -- the check itself is read-only.

REVIT API USED (see the comments next to each call for details)
---------------------------------------------------------------
FilteredElementCollector(doc)
    .OfCategory(BuiltInCategory.OST_ElectricalEquipment)
    .WhereElementIsNotElementType()      -> panel instances
    .OfClass(ElectricalSystem)           -> every circuit in the model
ElectricalSystem.BaseEquipment           -> the panel a circuit is fed from
ElectricalSystem.CircuitNumber           -> circuit number text
Element.get_Parameter(BuiltInParameter)  -> language-independent lookup
Element.LookupParameter(name)            -> fallback lookup by display name
Parameter.StorageType / HasValue / AsDouble / AsInteger / AsString
Parameter.IsReadOnly / Parameter.Set     -> writing the new MCB Rating
UnitUtils.ConvertFromInternalUnits / ConvertToInternalUnits + UnitTypeId.Amperes
WorksharingUtils.GetCheckoutStatus       -> skip panels owned by someone else
Document.GetElement(panel.LevelId)       -> level name for the Level filter
UIDocument.Selection.GetElementIds()     -> "Tick Revit selection"
Transaction (through pyRevit's revit.Transaction context manager)

Compatible with Revit 2022 - 2025 (IronPython 2.7 engine).
"""

__title__ = "Panel MCB\nCheck"
__author__ = 'BHEEM'

import os

from pyrevit import revit, DB, forms, script
from Autodesk.Revit.DB.Electrical import ElectricalSystem

from System.Windows import RoutedEventHandler
from System.Windows.Controls.Primitives import ButtonBase
from System.Windows.Input import Key
import safetable  # crash-safe output tables (lib/safetable.py)


# ---------------------------------------------------------------------------
# Settings you may want to change
# ---------------------------------------------------------------------------

# Standard incomer ratings to choose from, in amperes, smallest first.
STANDARD_RATINGS = [32, 40, 63, 80, 100, 125]

# Display names, used only as a fallback when the built-in parameter cannot
# be found (for example a family that carries its own shared parameter).
PANEL_MCB_PARAM_NAME = "MCB Rating"
CIRCUIT_RATING_PARAM_NAME = "Rating"

# Revit has no built-in "Panel Number" on Electrical Equipment, so the
# report uses the instance Mark.  Put a parameter name here (for example
# "Panel Number") to read that instead; Mark is used when it is blank.
PANEL_NUMBER_PARAM_NAME = ""

# Two ratings closer than this (in amperes) are treated as equal, so a
# 63 A circuit never "exceeds" a 63 A MCB because of floating point noise.
TOLERANCE = 0.001

STATUS_OK = "OK"
STATUS_UPGRADE = "Upgrade Required"
STATUS_EXCEEDS = "Exceeds Maximum Standard Rating"
STATUS_NOT_SET = "MCB Rating Not Set"

TITLE = "Panel MCB Check"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def element_id_value(element_id):
    """ElementId -> int.  .Value exists from Revit 2024, .IntegerValue is
    the only option in 2022/2023 (and is removed in 2026)."""
    try:
        return int(element_id.Value)
    except Exception:
        return int(element_id.IntegerValue)


def internal_to_amps(value):
    """Convert a Revit internal current value to amperes.

    UnitUtils.ConvertFromInternalUnits(value, UnitTypeId.Amperes) is the
    Revit 2021+ unit API (ForgeTypeId based).  Revit already stores current
    in amperes, so this is effectively 1:1 -- but going through UnitUtils
    keeps the script correct whatever the project's display units are.
    """
    try:
        return DB.UnitUtils.ConvertFromInternalUnits(value, DB.UnitTypeId.Amperes)
    except Exception:
        return value


def amps_to_internal(value):
    """Inverse of internal_to_amps(), used when writing the MCB Rating."""
    try:
        return DB.UnitUtils.ConvertToInternalUnits(value, DB.UnitTypeId.Amperes)
    except Exception:
        return value


def find_parameter(element, built_in, name):
    """Return a parameter by BuiltInParameter, falling back to its name.

    Element.get_Parameter(BuiltInParameter) works in every Revit language.
    Element.LookupParameter(name) matches the display name and is only
    used when the built-in one is missing on that element.
    """
    parameter = None
    if built_in is not None:
        try:
            parameter = element.get_Parameter(built_in)
        except Exception:
            parameter = None
    if parameter is None and name:
        try:
            parameter = element.LookupParameter(name)
        except Exception:
            parameter = None
    return parameter


def text_to_amps(text):
    """'63 A', '63A', '63.0' -> 63.0.  Returns None when it is not a number."""
    cleaned = (text or "").strip().upper().replace("AMPS", "").replace("AMP", "")
    cleaned = cleaned.replace("A", "").replace(",", ".").strip()
    try:
        return float(cleaned)
    except Exception:
        return None


def parameter_amps(parameter):
    """Read a current rating from a parameter as amperes.

    Returns None when the parameter is missing, empty, not a number or
    not greater than zero -- the caller treats None as "no valid rating".

    Parameter.StorageType tells us how the value is stored:
        Double  -> AsDouble()  (internal units, converted to amperes)
        Integer -> AsInteger()
        String  -> AsString()  (parsed, so "63 A" still works)
    """
    if parameter is None:
        return None
    try:
        if not parameter.HasValue:
            return None
        storage = parameter.StorageType
        if storage == DB.StorageType.Double:
            amps = internal_to_amps(parameter.AsDouble())
        elif storage == DB.StorageType.Integer:
            amps = float(parameter.AsInteger())
        elif storage == DB.StorageType.String:
            amps = text_to_amps(parameter.AsString())
        else:
            return None
    except Exception:
        return None

    if amps is None or amps <= TOLERANCE:
        return None
    return float(amps)


def parameter_text(parameter):
    if parameter is None:
        return ""
    try:
        return parameter.AsString() or parameter.AsValueString() or ""
    except Exception:
        return ""


def format_amps(amps):
    """63.0 -> '63 A', 62.5 -> '62.5 A', None -> '-'."""
    if amps is None:
        return "-"
    return "{:g} A".format(round(amps, 2))


def suggest_rating(highest_circuit_amps):
    """Next standard rating strictly ABOVE the highest circuit Rating
    (the MCB Rating must be more than every circuit Rating).

        35 -> 40      55 -> 63      78 -> 80      120 -> 125
        63 -> 80 (equal is not enough)            125 -> None (too big)
    """
    if highest_circuit_amps is None:
        return None
    for rating in sorted(STANDARD_RATINGS):
        if rating > highest_circuit_amps + TOLERANCE:
            return rating
    return None


# ---------------------------------------------------------------------------
# Reading the model
# ---------------------------------------------------------------------------

def collect_panels(doc):
    """Every placed Electrical Equipment family instance.

    FilteredElementCollector(doc) searches the whole model.
    .OfCategory(OST_ElectricalEquipment) keeps the panel category.
    .WhereElementIsNotElementType() drops the family TYPES, keeping the
    placed instances.
    """
    collector = DB.FilteredElementCollector(doc) \
        .OfCategory(DB.BuiltInCategory.OST_ElectricalEquipment) \
        .WhereElementIsNotElementType()
    return [element for element in collector if isinstance(element, DB.FamilyInstance)]


def collect_circuits_by_panel(doc):
    """{panel element id: [ElectricalSystem, ...]} for the whole model.

    One pass over every circuit is much faster than asking each panel in
    turn.  ElectricalSystem.BaseEquipment is the panel the circuit is FED
    FROM, which is exactly "circuits connected to the panel" for this
    check.  (Per panel, the equivalent call is
    panel.MEPModel.GetAssignedElectricalSystems().)
    """
    by_panel = {}
    for circuit in DB.FilteredElementCollector(doc).OfClass(ElectricalSystem):
        try:
            panel = circuit.BaseEquipment
        except Exception:
            panel = None
        if panel is None:
            continue    # circuit not assigned to any panel
        by_panel.setdefault(element_id_value(panel.Id), []).append(circuit)
    return by_panel


def circuit_number(circuit):
    try:
        return circuit.CircuitNumber or ""
    except Exception:
        return parameter_text(find_parameter(
            circuit, DB.BuiltInParameter.RBS_ELEC_CIRCUIT_NUMBER, None))


def panel_name(panel):
    name = parameter_text(find_parameter(
        panel, DB.BuiltInParameter.RBS_ELEC_PANEL_NAME, "Panel Name"))
    if name:
        return name
    try:
        return panel.Name or ""
    except Exception:
        return ""


def panel_number(panel):
    if PANEL_NUMBER_PARAM_NAME:
        text = parameter_text(find_parameter(panel, None, PANEL_NUMBER_PARAM_NAME))
        if text:
            return text
    return parameter_text(find_parameter(
        panel, DB.BuiltInParameter.ALL_MODEL_MARK, "Mark"))


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------

class CircuitInfo(object):
    """One circuit that has a valid Rating."""

    def __init__(self, element, number, amps):
        self.element = element
        self.number = number
        self.amps = amps


class PanelResult(object):
    """Outcome of the check for one panel."""

    def __init__(self, panel):
        self.panel = panel
        self.name = panel_name(panel)
        self.number = panel_number(panel)
        self.mcb_parameter = find_parameter(
            panel, DB.BuiltInParameter.RBS_ELEC_PANEL_MCB_RATING_PARAM,
            PANEL_MCB_PARAM_NAME)
        self.mcb_amps = parameter_amps(self.mcb_parameter)
        self.circuits = []        # CircuitInfo, valid ratings only
        self.ignored = 0          # circuits skipped: no / invalid Rating
        self.offenders = []       # circuits whose Rating > MCB Rating
        self.highest = None       # CircuitInfo with the largest Rating
        self.suggested = None     # standard rating in amperes, or None
        self.status = STATUS_OK

    @property
    def label(self):
        """Text shown in the 'choose panels' list."""
        return "{}  ({})   {}  ->  {}".format(
            self.name or "<unnamed panel>", self.number or "no number",
            format_amps(self.mcb_amps), format_amps(self.suggested))

    @property
    def can_update(self):
        return (self.status in (STATUS_UPGRADE, STATUS_NOT_SET)
                and self.suggested is not None
                and self.mcb_parameter is not None)


def check_panel(panel, circuits):
    """Apply requirements 2 - 7 to one panel and return a PanelResult."""
    result = PanelResult(panel)

    # --- circuits with a valid Rating ------------------------------------
    for circuit in circuits:
        amps = parameter_amps(find_parameter(
            circuit, DB.BuiltInParameter.RBS_ELEC_CIRCUIT_RATING_PARAM,
            CIRCUIT_RATING_PARAM_NAME))
        if amps is None:
            result.ignored += 1       # empty or invalid Rating -> ignore
            continue
        result.circuits.append(CircuitInfo(circuit, circuit_number(circuit), amps))

    if not result.circuits:
        # Nothing to compare against: the panel cannot be undersized.
        return result

    result.circuits.sort(key=lambda info: info.amps, reverse=True)
    result.highest = result.circuits[0]
    result.suggested = suggest_rating(result.highest.amps)

    # --- compare each circuit individually with the MCB Rating -----------
    if result.mcb_amps is None:
        # No incomer rating to compare with: every rated circuit is unchecked.
        result.status = STATUS_NOT_SET
        if result.suggested is None:
            result.status = STATUS_EXCEEDS
        return result

    result.offenders = [info for info in result.circuits
                        if info.amps >= result.mcb_amps - TOLERANCE]
    if not result.offenders:
        result.suggested = None       # nothing to suggest, the MCB is adequate
        return result

    if result.suggested is None:
        result.status = STATUS_EXCEEDS    # highest circuit is above 125 A
    else:
        result.status = STATUS_UPGRADE
    return result


def analyse(doc):
    """Check every panel, showing a cancellable progress bar."""
    panels = collect_panels(doc)
    if not panels:
        return [], False

    circuits_by_panel = collect_circuits_by_panel(doc)
    results = []
    cancelled = False
    total = len(panels)

    with forms.ProgressBar(title="Checking panels ({value} of {max_value})",
                           cancellable=True) as progress:
        for index, panel in enumerate(panels):
            if progress.cancelled:
                cancelled = True
                break
            try:
                circuits = circuits_by_panel.get(element_id_value(panel.Id), [])
                results.append(check_panel(panel, circuits))
            except Exception as error:
                # One broken panel must not stop the whole check.
                logger.warning("Panel id {} could not be checked: {}".format(
                    element_id_value(panel.Id), error))
            progress.update_progress(index + 1, total)

    results.sort(key=lambda r: ((r.name or "").lower(), (r.number or "").lower()))
    return results, cancelled


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

STATUS_COLOURS = {
    STATUS_OK: "#1e8e5a",
    STATUS_UPGRADE: "#c96a00",
    STATUS_EXCEEDS: "#c0392b",
    STATUS_NOT_SET: "#7a4bb5",
}


def status_cell(status):
    return '<span style="color:{};font-weight:bold">{}</span>'.format(
        STATUS_COLOURS.get(status, "#333333"), status)


def suggested_text(result):
    if result.status == STATUS_EXCEEDS:
        return "> {:g} A".format(max(STANDARD_RATINGS))
    return format_amps(result.suggested)


def table_rows(result):
    """Rows for one panel.

    A flagged panel gets one row per circuit that causes the issue.
    Any other panel gets a single row showing its highest rated circuit.
    """
    # output.linkify() makes a clickable link that selects the element.
    name = output.linkify(result.panel.Id, title=result.name or "<unnamed panel>")

    if result.offenders:
        listed = result.offenders
    elif result.highest is not None:
        listed = [result.highest]
    else:
        listed = []

    if not listed:
        return [[name, result.number or "-", format_amps(result.mcb_amps),
                 "-", "no rated circuits", "-", status_cell(result.status)]]

    rows = []
    for info in listed:
        rows.append([
            name,
            result.number or "-",
            format_amps(result.mcb_amps),
            output.linkify(info.element.Id, title=info.number or "?"),
            format_amps(info.amps),
            suggested_text(result),
            status_cell(result.status),
        ])
    return rows


def print_report(results, cancelled):
    output.set_title(TITLE)
    output.print_md("# " + TITLE)
    output.print_md("Model: **{}**".format(revit.doc.Title))
    if cancelled:
        output.print_md("**Cancelled - the table below is incomplete.**")

    counts = {}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    summary = ["{} panel(s) checked".format(len(results))]
    for status in (STATUS_OK, STATUS_UPGRADE, STATUS_EXCEEDS, STATUS_NOT_SET):
        if counts.get(status):
            summary.append("{}: {}".format(status, counts[status]))
    output.print_md("  |  ".join(summary))

    # Problems first, then the rest.
    order = {STATUS_EXCEEDS: 0, STATUS_UPGRADE: 1, STATUS_NOT_SET: 2, STATUS_OK: 3}
    rows = []
    for result in sorted(results, key=lambda r: order.get(r.status, 9)):
        rows.extend(table_rows(result))

    safetable.print_table(output, 
        table_data=rows,
        columns=["Panel Name", "Panel Number", "Existing MCB Rating",
                 "Circuit Number", "Circuit Rating", "Suggested MCB Rating",
                 "Status"],
        title="Panel MCB Rating vs connected circuit Ratings"
    )
    output.print_md(
        "Standard ratings: {} A. A flagged panel lists every circuit whose "
        "Rating is equal to or above its MCB Rating; other panels show their highest "
        "rated circuit. Click a panel or circuit to select it in Revit."
        .format(", ".join("{:g}".format(r) for r in STANDARD_RATINGS)))

    ignored = sum(result.ignored for result in results)
    if ignored:
        output.print_md("{} circuit(s) were ignored because their Rating is "
                        "empty or not a valid number.".format(ignored))


# ---------------------------------------------------------------------------
# Optional update
# ---------------------------------------------------------------------------

def owned_by_someone_else(doc, element):
    """In a workshared model, True when another user has the panel checked
    out -- writing to it would fail, so it is skipped and reported.

    WorksharingUtils.GetCheckoutStatus(doc, id) returns a CheckoutStatus.
    """
    try:
        if not doc.IsWorkshared:
            return False
        status = DB.WorksharingUtils.GetCheckoutStatus(doc, element.Id)
        return status == DB.CheckoutStatus.OwnedByOtherUser
    except Exception:
        return False


def set_mcb_rating(result):
    """Write result.suggested into the panel's MCB Rating parameter.

    Parameter.Set() must match the parameter's StorageType, and a Double
    has to be given in Revit internal units.  Raises on failure.
    """
    parameter = result.mcb_parameter
    if parameter is None:
        raise ValueError("the panel has no MCB Rating parameter")
    if parameter.IsReadOnly:
        raise ValueError("MCB Rating is read-only on this panel")

    storage = parameter.StorageType
    if storage == DB.StorageType.Double:
        done = parameter.Set(amps_to_internal(float(result.suggested)))
    elif storage == DB.StorageType.Integer:
        done = parameter.Set(int(result.suggested))
    elif storage == DB.StorageType.String:
        done = parameter.Set(str(result.suggested))
    else:
        raise ValueError("MCB Rating has an unsupported storage type")
    if not done:
        raise ValueError("Revit rejected the new value")


def update_panels(doc, chosen):
    """Write the suggested ratings inside ONE transaction.

    revit.Transaction is pyRevit's context manager around
    Autodesk.Revit.DB.Transaction: it starts the transaction, commits it
    when the block ends, and rolls it back if the block raises.

    The window is MODAL (show_dialog), so its button handlers still run
    inside the pushbutton command's API context -- a Transaction can be
    opened directly, no ExternalEvent is needed.
    """
    updated = []
    failed = []
    with revit.Transaction("Update panel MCB Rating"):
        for result in chosen:
            label = "{} ({})".format(result.name or "<unnamed panel>",
                                     result.number or "no number")
            try:
                if owned_by_someone_else(doc, result.panel):
                    raise ValueError("checked out by another user")
                set_mcb_rating(result)
                updated.append((result, label))
            except Exception as error:
                # Keep going: one locked panel must not undo the others.
                failed.append((label, str(error)))
    return updated, failed


# ---------------------------------------------------------------------------
# Window: rows shown in the two grids
# ---------------------------------------------------------------------------

STATUS_NOT_CHECKED = "Not checked"

ALL_LEVELS = "<All levels>"
NO_LEVEL = "<No level>"
NO_TYPE = "<No type>"
ALL_STATUSES = "<Any status>"

# status -> (text colour, chip background)
STATUS_STYLE = {
    STATUS_OK: ("#FF116D4A", "#FFE3F4EC"),
    STATUS_UPGRADE: ("#FF9A5200", "#FFFDEFD9"),
    STATUS_EXCEEDS: ("#FFA5281B", "#FFFBE4E1"),
    STATUS_NOT_SET: ("#FF5E3A94", "#FFEEE6F8"),
    STATUS_NOT_CHECKED: ("#FF667585", "#FFEEF2F6"),
}


def panel_level(doc, panel):
    """Level name of a panel.  Document.GetElement(panel.LevelId) is the
    normal route; the Level parameter covers families hosted differently."""
    try:
        level = doc.GetElement(panel.LevelId)
        if level is not None and level.Name:
            return level.Name
    except Exception:
        pass
    return parameter_text(find_parameter(
        panel, DB.BuiltInParameter.FAMILY_LEVEL_PARAM, "Level"))


def panel_type(panel):
    """'Family : Type' text, read from ELEM_FAMILY_AND_TYPE_PARAM."""
    return parameter_text(find_parameter(
        panel, DB.BuiltInParameter.ELEM_FAMILY_AND_TYPE_PARAM, None))


class PanelRow(object):
    """One line in the panel grid.  Plain attributes so WPF can bind to them."""

    def __init__(self, doc, panel, circuits):
        self.IsSelected = False
        self.panel = panel
        self.circuits = circuits            # ElectricalSystems fed by the panel
        self.Name = panel_name(panel) or "<unnamed panel>"
        self.Number = panel_number(panel)
        self.Level = panel_level(doc, panel)
        self.TypeName = panel_type(panel)
        self.CircuitCount = len(circuits)
        self.result = None
        self.read_mcb()
        self.clear_result()
        self.SearchText = " | ".join(
            [self.Name, self.Number, self.Level, self.TypeName]).lower()

    def read_mcb(self):
        self.McbText = format_amps(parameter_amps(find_parameter(
            self.panel, DB.BuiltInParameter.RBS_ELEC_PANEL_MCB_RATING_PARAM,
            PANEL_MCB_PARAM_NAME)))

    def _set_status(self, status):
        self.Status = status
        self.StatusColor, self.StatusBack = STATUS_STYLE.get(
            status, STATUS_STYLE[STATUS_NOT_CHECKED])

    def clear_result(self):
        self.result = None
        self.HighestText = "-"
        self.SuggestedText = "-"
        self.Issues = ""
        self._set_status(STATUS_NOT_CHECKED)

    def apply(self, result):
        """Show the outcome of check_panel() for this panel."""
        self.result = result
        self.McbText = format_amps(result.mcb_amps)
        if result.highest is not None:
            self.HighestText = "{}  (cct {})".format(
                format_amps(result.highest.amps), result.highest.number or "?")
        else:
            self.HighestText = "no rated circuits"
        self.SuggestedText = suggested_text(result) if result.status != STATUS_OK else "-"
        self.Issues = ", ".join(info.number or "?" for info in result.offenders)
        self._set_status(result.status)

    @property
    def can_update(self):
        return self.result is not None and self.result.can_update


class TypeOption(object):
    """One tickable line in the panel-type list."""

    def __init__(self, name, count):
        self.IsSelected = False
        self.Name = name
        self.Count = count
        self.Label = "{}  ({})".format(name, count)


class CircuitRow(object):
    """One line in the 'circuits of the highlighted panel' grid."""

    def __init__(self, number, load_name, rating_text, verdict, colour):
        self.Number = number
        self.LoadName = load_name
        self.RatingText = rating_text
        self.Verdict = verdict
        self.VerdictColor = colour


def circuit_load_name(circuit):
    return parameter_text(find_parameter(
        circuit, DB.BuiltInParameter.RBS_ELEC_CIRCUIT_NAME, "Load Name"))


def parse_ratings(text):
    """'32, 40 63;80' -> [32.0, 40.0, 63.0, 80.0].  Raises ValueError."""
    values = []
    for part in (text or "").replace(";", ",").replace(" ", ",").split(","):
        part = part.strip()
        if not part:
            continue
        amps = text_to_amps(part)
        if amps is None or amps <= 0:
            raise ValueError("'{}' is not a valid rating.".format(part))
        values.append(amps)
    if not values:
        raise ValueError("Enter at least one standard rating.")
    return sorted(set(values))


def build_rows(doc):
    """Read every panel once (name, level, type, MCB, circuit count)."""
    circuits_by_panel = collect_circuits_by_panel(doc)
    rows = []
    for panel in collect_panels(doc):
        try:
            circuits = circuits_by_panel.get(element_id_value(panel.Id), [])
            rows.append(PanelRow(doc, panel, circuits))
        except Exception as error:
            logger.warning("Panel id {} could not be read: {}".format(
                element_id_value(panel.Id), error))
    rows.sort(key=lambda row: (row.Name.lower(), (row.Number or "").lower()))
    return rows


# ---------------------------------------------------------------------------
# Window
# ---------------------------------------------------------------------------

class PanelMcbWindow(forms.WPFWindow):
    """Pick panels, filter them, run the check, optionally update the model."""

    def __init__(self, doc, uidoc, rows):
        forms.WPFWindow.__init__(
            self, os.path.join(os.path.dirname(__file__), "PanelMcbCheck.xaml"))
        self.doc = doc
        self.uidoc = uidoc
        self.rows = rows            # every panel in the model
        self.shown = []             # the rows the filters currently show
        self.report_requested = False
        self._loading = True

        self.txtDocName.Text = doc.Title
        self.txtRatings.Text = ", ".join("{:g}".format(r) for r in STANDARD_RATINGS)
        self._fill_filter_lists()
        self._wire_events()
        self._loading = False

        self.apply_filter()
        self._show_circuits(None)

    # ---- set-up ----------------------------------------------------------

    def _fill_filter_lists(self):
        levels = sorted(set(r.Level for r in self.rows if r.Level), key=lambda v: v.lower())
        level_items = [ALL_LEVELS] + levels
        if any(not r.Level for r in self.rows):
            level_items.append(NO_LEVEL)
        self.cmbLevel.ItemsSource = level_items
        self.cmbLevel.SelectedIndex = 0

        # Panel types: a tick list (multi-select) with the panel count of each.
        counts = {}
        for row in self.rows:
            key = row.TypeName or NO_TYPE
            counts[key] = counts.get(key, 0) + 1
        self.type_options = [TypeOption(name, counts[name])
                             for name in sorted(counts, key=lambda v: v.lower())]
        self.lstTypes.ItemsSource = self.type_options

        self.cmbStatus.ItemsSource = [
            ALL_STATUSES, STATUS_NOT_CHECKED, STATUS_OK, STATUS_UPGRADE,
            STATUS_EXCEEDS, STATUS_NOT_SET]
        self.cmbStatus.SelectedIndex = 0

    def _wire_events(self):
        self.txtSearch.TextChanged += self.on_filter_changed
        self.cmbLevel.SelectionChanged += self.on_filter_changed
        # Tick boxes inside the type list bubble their Click up to the list.
        self.lstTypes.AddHandler(ButtonBase.ClickEvent, RoutedEventHandler(self.on_filter_changed))
        self.btnTypesAll.Click += self.on_types_all
        self.btnTypesNone.Click += self.on_types_none
        self.btnTickTypes.Click += self.on_tick_types
        self.cmbStatus.SelectionChanged += self.on_filter_changed
        self.chkWithCircuits.Click += self.on_filter_changed
        self.chkOnlyTicked.Click += self.on_filter_changed
        self.btnClearSearch.Click += self.on_clear_search
        self.btnResetFilters.Click += self.on_reset_filters

        self.chkTickAll.Click += self.on_tick_all
        self.btnClearTicks.Click += self.on_clear_ticks
        self.btnTickSelection.Click += self.on_tick_revit_selection

        self.dgPanels.SelectionChanged += self.on_row_highlighted
        self.dgPanels.PreviewKeyDown += self.on_grid_key
        # Tick-box clicks bubble up from the template column's CheckBox.
        self.dgPanels.AddHandler(ButtonBase.ClickEvent, RoutedEventHandler(self.on_grid_click))

        self.btnCheck.Click += self.on_check
        self.btnUpdate.Click += self.on_update
        self.btnReport.Click += self.on_report
        self.btnClose.Click += self.on_close

    # ---- filtering -------------------------------------------------------

    def _combo(self, combo, default):
        try:
            return str(combo.SelectedItem or default)
        except Exception:
            return default

    def _matches(self, row, words, level, types, status, with_circuits, only_ticked):
        if only_ticked and not row.IsSelected:
            return False
        if with_circuits and not row.CircuitCount:
            return False
        if level == NO_LEVEL:
            if row.Level:
                return False
        elif level != ALL_LEVELS and row.Level != level:
            return False
        if types and (row.TypeName or NO_TYPE) not in types:
            return False
        if status != ALL_STATUSES and row.Status != status:
            return False
        for word in words:
            if word not in row.SearchText:
                return False
        return True

    def apply_filter(self):
        """Re-evaluate every filter and rebind the panel grid.
        Pure Python on cached strings -- no Revit API calls while typing."""
        words = (self.txtSearch.Text or "").lower().split()
        level = self._combo(self.cmbLevel, ALL_LEVELS)
        types = self.ticked_types()      # empty set = every type
        status = self._combo(self.cmbStatus, ALL_STATUSES)
        with_circuits = bool(self.chkWithCircuits.IsChecked)
        only_ticked = bool(self.chkOnlyTicked.IsChecked)

        highlighted = self.dgPanels.SelectedItem
        self.shown = [row for row in self.rows if self._matches(
            row, words, level, types, status, with_circuits, only_ticked)]
        self.dgPanels.ItemsSource = None
        self.dgPanels.ItemsSource = self.shown
        if highlighted is not None and any(row is highlighted for row in self.shown):
            self.dgPanels.SelectedItem = highlighted
        self.refresh_state()

    def on_filter_changed(self, sender, args):
        if not self._loading:
            self.apply_filter()

    # ---- panel type tick list --------------------------------------------

    def ticked_types(self):
        return set(option.Name for option in self.type_options if option.IsSelected)

    def _set_types(self, state):
        for option in self.type_options:
            option.IsSelected = state
        self.lstTypes.Items.Refresh()

    def on_types_all(self, sender, args):
        self._set_types(True)
        self.apply_filter()

    def on_types_none(self, sender, args):
        self._set_types(False)
        self.apply_filter()

    def on_tick_types(self, sender, args):
        """Tick every panel of the ticked types, whatever the other filters show."""
        types = self.ticked_types()
        if not types:
            forms.alert("Tick one or more panel types in the list first.", title=TITLE)
            return
        self._set_ticks([row for row in self.rows
                         if (row.TypeName or NO_TYPE) in types], True)

    def on_clear_search(self, sender, args):
        self.txtSearch.Text = ""
        self.txtSearch.Focus()

    def on_reset_filters(self, sender, args):
        self._loading = True
        try:
            self.txtSearch.Text = ""
            self.cmbLevel.SelectedIndex = 0
            self._set_types(False)
            self.cmbStatus.SelectedIndex = 0
            self.chkWithCircuits.IsChecked = False
            self.chkOnlyTicked.IsChecked = False
        finally:
            self._loading = False
        self.apply_filter()

    # ---- ticks -----------------------------------------------------------

    def ticked(self):
        return [row for row in self.rows if row.IsSelected]

    def refresh_state(self):
        """Counts, tick-all box, button states, footer text."""
        ticked = self.ticked()
        shown_ids = set(id(row) for row in self.shown)
        hidden = len([row for row in ticked if id(row) not in shown_ids])

        chosen = len(self.ticked_types())
        self.txtTypeLabel.Text = ("PANEL TYPE  ({} ticked)".format(chosen)
                                  if chosen else "PANEL TYPE  (all)")
        text = "{} of {} panel(s) shown".format(len(self.shown), len(self.rows))
        self.txtShown.Text = text
        badge = "Ticked : {}".format(len(ticked))
        if hidden:
            badge += "   ({} hidden by the filters)".format(hidden)
        self.txtTicked.Text = badge

        shown_ticked = len([row for row in self.shown if row.IsSelected])
        if not self.shown or not shown_ticked:
            self.chkTickAll.IsChecked = False
        elif shown_ticked == len(self.shown):
            self.chkTickAll.IsChecked = True
        else:
            self.chkTickAll.IsChecked = None      # some

        updatable = len([row for row in ticked if row.can_update])
        self.btnCheck.IsEnabled = bool(ticked)
        self.btnCheck.Content = ("Check {} ticked panel(s)".format(len(ticked))
                                 if ticked else "Check ticked panels")
        self.btnUpdate.IsEnabled = bool(updatable)
        self.btnUpdate.Content = ("Update MCB Rating ({})".format(updatable)
                                  if updatable else "Update MCB Rating")
        self.btnReport.IsEnabled = any(row.result is not None for row in self.rows)

        checked = [row for row in self.rows if row.result is not None]
        if not checked:
            self.txtFooter.Text = ("Tick the panels to check, then press "
                                   "'Check ticked panels'. Nothing is changed by the check.")
        else:
            counts = {}
            for row in checked:
                counts[row.Status] = counts.get(row.Status, 0) + 1
            parts = ["{} checked".format(len(checked))]
            for status in (STATUS_OK, STATUS_UPGRADE, STATUS_EXCEEDS, STATUS_NOT_SET):
                if counts.get(status):
                    parts.append("{}: {}".format(status, counts[status]))
            self.txtFooter.Text = "    |    ".join(parts)

    def _set_ticks(self, rows, state):
        for row in rows:
            row.IsSelected = state
        if self.chkOnlyTicked.IsChecked:
            self.apply_filter()           # the visible set itself changed
        else:
            self.dgPanels.Items.Refresh()
            self.refresh_state()

    def on_tick_all(self, sender, args):
        """Tick every panel shown -- or untick them when all are ticked."""
        if not self.shown:
            self.refresh_state()
            return
        self._set_ticks(self.shown, not all(row.IsSelected for row in self.shown))

    def on_clear_ticks(self, sender, args):
        self._set_ticks(self.rows, False)

    def on_tick_revit_selection(self, sender, args):
        """Tick the panels that are selected in Revit right now.
        UIDocument.Selection.GetElementIds() returns the current selection."""
        try:
            selected = set(element_id_value(i) for i in self.uidoc.Selection.GetElementIds())
        except Exception:
            selected = set()
        matches = [row for row in self.rows if element_id_value(row.panel.Id) in selected]
        if not matches:
            forms.alert("No electrical panels are selected in Revit.\n\n"
                        "Select panels in the model before starting the tool, "
                        "then use this button.", title=TITLE)
            return
        self._set_ticks(matches, True)

    def _highlighted(self):
        return [row for row in self.dgPanels.SelectedItems if isinstance(row, PanelRow)]

    def on_grid_click(self, sender, args):
        """A tick box was clicked.  If several rows are highlighted and the
        clicked one is among them, give them all the same tick."""
        row = getattr(args.OriginalSource, "DataContext", None)
        if not isinstance(row, PanelRow):
            return
        highlighted = self._highlighted()
        if len(highlighted) > 1 and any(item is row for item in highlighted):
            self._set_ticks(highlighted, bool(row.IsSelected))
        elif self.chkOnlyTicked.IsChecked:
            self.apply_filter()
        else:
            self.refresh_state()

    def on_grid_key(self, sender, args):
        """Space toggles the ticks of every highlighted row."""
        if args.Key != Key.Space:
            return
        highlighted = self._highlighted()
        if not highlighted:
            return
        self._set_ticks(highlighted, not all(row.IsSelected for row in highlighted))
        args.Handled = True

    # ---- circuit detail --------------------------------------------------

    def on_row_highlighted(self, sender, args):
        row = self.dgPanels.SelectedItem
        self._show_circuits(row if isinstance(row, PanelRow) else None)

    def _verdict(self, info, result, flagged):
        """Why a circuit is (not) a problem: the MCB Rating must be MORE
        than the circuit Rating, so 'equal' is flagged as well."""
        if not flagged:
            return "OK"
        if abs(info.amps - result.mcb_amps) <= TOLERANCE:
            return "Equal to MCB Rating"
        return "Exceeds MCB Rating"

    def _show_circuits(self, row):
        """List the circuits of the highlighted panel under the grid."""
        if row is None:
            self.txtDetailTitle.Text = "Circuits"
            self.txtDetailHint.Text = "Highlight a panel to list its circuits."
            self.dgCircuits.ItemsSource = None
            return

        self.txtDetailTitle.Text = "Circuits fed by {}".format(row.Name)
        lines = []
        if row.result is None:
            # Not checked yet: show the raw circuits and their ratings.
            for circuit in row.circuits:
                amps = parameter_amps(find_parameter(
                    circuit, DB.BuiltInParameter.RBS_ELEC_CIRCUIT_RATING_PARAM,
                    CIRCUIT_RATING_PARAM_NAME))
                lines.append((amps or 0, CircuitRow(
                    circuit_number(circuit), circuit_load_name(circuit),
                    format_amps(amps) if amps else "no rating",
                    "not checked" if amps else "ignored", "#FF667585")))
            self.txtDetailHint.Text = "{} circuit(s). Not checked yet.".format(len(lines))
        else:
            offenders = set(id(info) for info in row.result.offenders)
            for info in row.result.circuits:
                exceeds = id(info) in offenders
                lines.append((info.amps, CircuitRow(
                    info.number, circuit_load_name(info.element), format_amps(info.amps),
                    self._verdict(info, row.result, exceeds),
                    "#FFA5281B" if exceeds else "#FF116D4A")))
            self.txtDetailHint.Text = (
                "MCB Rating {}.  {} rated circuit(s), {} not below the MCB Rating, "
                "{} ignored (no valid Rating).".format(
                    format_amps(row.result.mcb_amps), len(row.result.circuits),
                    len(row.result.offenders), row.result.ignored))
        lines.sort(key=lambda pair: pair[0], reverse=True)
        self.dgCircuits.ItemsSource = [line for _, line in lines]

    # ---- actions ---------------------------------------------------------

    def _run_check(self, rows):
        """check_panel() for each row, with a cancellable progress bar."""
        total = len(rows)
        with forms.ProgressBar(title="Checking panels ({value} of {max_value})",
                               cancellable=True) as progress:
            for index, row in enumerate(rows):
                if progress.cancelled:
                    return False
                try:
                    row.apply(check_panel(row.panel, row.circuits))
                except Exception as error:
                    row.clear_result()
                    logger.warning("Panel '{}' could not be checked: {}".format(row.Name, error))
                progress.update_progress(index + 1, total)
        return True

    def on_check(self, sender, args):
        global STANDARD_RATINGS
        rows = self.ticked()
        if not rows:
            forms.alert("Tick at least one panel to check.", title=TITLE)
            return
        try:
            STANDARD_RATINGS = parse_ratings(self.txtRatings.Text)
        except ValueError as error:
            forms.alert("Standard ratings: {}".format(error), title=TITLE)
            self.txtRatings.Focus()
            return

        self._run_check(rows)
        self.apply_filter()
        self._show_circuits(self.dgPanels.SelectedItem
                            if isinstance(self.dgPanels.SelectedItem, PanelRow) else None)

    def on_update(self, sender, args):
        rows = [row for row in self.ticked() if row.can_update]
        if not rows:
            return
        confirmed = forms.alert(
            "Write the suggested MCB Rating to {} ticked panel(s)?\n\n"
            "This CHANGES the model (one transaction, undo with Ctrl+Z). "
            "Panels that are OK or above {:g} A are not touched.".format(
                len(rows), max(STANDARD_RATINGS)),
            title=TITLE, yes=True, no=True)
        if not confirmed:
            return

        try:
            updated, failed = update_panels(self.doc, [row.result for row in rows])
        except Exception as error:
            # revit.Transaction already rolled everything back.
            forms.alert("The update was rolled back:\n\n{}".format(error), title=TITLE)
            return

        # Re-read and re-check so the grid shows the model as it is now.
        for row in rows:
            row.read_mcb()
        self._run_check(rows)
        self.apply_filter()
        self._show_circuits(self.dgPanels.SelectedItem
                            if isinstance(self.dgPanels.SelectedItem, PanelRow) else None)

        message = "{} panel(s) updated.".format(len(updated))
        if failed:
            message += "\n\n{} panel(s) NOT updated:\n".format(len(failed))
            message += "\n".join("- {}: {}".format(label, reason)
                                 for label, reason in failed[:20])
        forms.alert(message, title=TITLE)

    def on_report(self, sender, args):
        """Close the window and print the table in the pyRevit output
        window (which cannot be used while this modal window is open)."""
        self.report_requested = True
        self.Close()

    def on_close(self, sender, args):
        self.Close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    doc = revit.doc
    if doc is None or doc.IsFamilyDocument:
        forms.alert("Open a project model first.", title=TITLE, exitscript=True)

    try:
        rows = build_rows(doc)
    except Exception as error:
        forms.alert("The panels could not be read:\n\n{}".format(error), title=TITLE)
        logger.exception(error)
        return
    if not rows:
        forms.alert("No electrical panels were found in this model.", title=TITLE)
        return

    window = PanelMcbWindow(doc, revit.uidoc, rows)
    window.show_dialog()

    if window.report_requested:
        results = [row.result for row in rows if row.result is not None]
        if results:
            print_report(results, False)


logger = script.get_logger()
output = script.get_output()

if __name__ == "__main__":
    main()
