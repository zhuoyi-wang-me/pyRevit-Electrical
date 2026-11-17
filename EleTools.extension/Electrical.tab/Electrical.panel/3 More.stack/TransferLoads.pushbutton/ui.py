# -*- coding: utf-8 -*-
"""WPF UI controller for the Load Transfer tool.

Required XAML controls (see LoadTransfer.xaml):
    cmbTemplates, btnLoadTemplate, btnSaveTemplate, btnDeleteTemplate,
    chkEquipment, chkFixtures, cmbDetailFamily,
    brdFixtureFilter, txtFixtureSearch, btnClearFixtureSearch, lstFixtureTypes,
    btnFixturesAll, btnFixturesNone, txtFixtureSummary,
    txtDetailSearch, btnClearDetailSearch, txtDetailSearchSummary,
    cmbMatchSource, cmbMatchTarget, btnPreview,
    cmbFilterField, cmbFilterMode, txtTransferFilter, chkFilterExclude,
    btnApplyFilter, btnIncludeAll, btnIncludeNone, txtFilterSummary,
    txtMatchSearch, btnSelectInModel, dgMatches,
    cmbPickSourceParam, cmbPickTargetParam, txtPickMultiplier,
    btnAddMapping, btnPreviewValues, dgParamMap, btnEditMapRow,
    btnRemoveMapRow, btnClearMapRows,
    txtStatus, btnTransfer

Two different narrowing mechanisms exist here and they are NOT the same
thing -- keeping them distinct is deliberate:

  * txtMatchSearch only filters what the grid DISPLAYS. It never
    affects what gets written.
  * The Transfer Filter (txtTransferFilter + the Include checkbox
    column) decides which matched rows are actually transferred.

Merging them would mean scrolling away from a row, or typing in a
search box, could silently change what gets written to the model.
"""

import os

from pyrevit import forms, revit

import logic
import preview
import templates


window = None
doc = None
uidoc = None

detail_groups = {}          # "Family : Type" -> [instances]
current_matches = []        # list of match dicts from logic.build_matches
all_match_rows = []         # unfiltered MatchRow list backing the search box
param_map_rows = []         # list of ParamMapRow
editing_row = None          # ParamMapRow currently loaded into the pickers
all_detail_keys = []        # every "Family : Type" key, unfiltered
suppress_detail_events = False   # True while the combo is being repopulated


class MatchRow(object):
    """One row in the Match Preview grid."""

    def __init__(self, match, preview=None):
        target = match["target"]
        source = match["source"]
        status = match.get("status", "matched" if match["matched"] else "unmatched")

        # Kept for "Select Source + Target in Model" -- not bound to any
        # DataGrid column, just carried along with the row.
        self._target_element = target
        self._source_element = source
        self._match = match

        # CanTransfer gates the Include checkbox: an ambiguous/unmatched
        # row has no safe source to read from, so it can never be ticked
        # on. Include is what actually decides whether this row is
        # written.
        self.CanTransfer = bool(match["matched"])
        self.Include = self.CanTransfer

        self.TargetName = logic.element_label(target)
        self.MatchValue = match["key"] or ""
        self.ValuePreview = _format_value_preview(preview)

        # RowKind drives the row colour in the Match Preview grid (see LoadTransfer.xaml).
        self.RowKind = {"matched": "matched", "ambiguous": "ambiguous"}.get(status, "unmatched")

        if status == "matched":
            self.SourceName = logic.element_label(source)
            self.Status = "Matched"
        elif status == "ambiguous":
            breakdown = match.get("ambiguous_breakdown") or ""
            if breakdown:
                self.SourceName = "AMBIGUOUS: {}".format(breakdown)
            else:
                self.SourceName = "({} sources share this value)".format(match.get("ambiguous_count", 0))
            self.Status = "AMBIGUOUS - not transferred"
        elif status == "no_key":
            self.SourceName = "(target has no value here)"
            self.Status = "No match value on target"
        else:
            self.SourceName = "(no match)"
            self.Status = "No source match"

    def set_preview(self, preview):
        """Refresh just the Value Preview text.

        Used instead of rebuilding MatchRow objects so that Include
        ticks the user has set by hand survive a second 'Preview
        Values' click -- rebuilding would quietly reset their choices.
        """
        self.ValuePreview = _format_value_preview(preview)
        if self.CanTransfer:
            blocked = bool(preview) and any(p.get("issue") for p in preview.get("previews") or [])
            self.RowKind = "blocked" if blocked else "matched"

    def filter_values(self, field):
        """The strings this row offers up to the Transfer Filter."""
        if field == "Target Name":
            return [self.TargetName]
        if field == "Source Name":
            return [self.SourceName]
        if field == "Any of these":
            return [self.MatchValue, self.TargetName, self.SourceName]
        return [self.MatchValue]


def _format_number(value):
    """Match-grid rendering. Delegates to logic.format_number so this
    window and the preview dialog can never show the same quantity two
    different ways."""
    if value is None:
        return "(empty)"
    text = logic.format_number(value)
    return text if text is not None else str(value)


def _format_value_preview(preview):
    """Turn a logic.preview_transfer() entry into one readable line per
    mapping, e.g. 'TCL: 0.00 -> 30.00' or 'TCL: BLOCKED - read-only'."""
    if not preview or not preview.get("previews"):
        return ""
    parts = []
    for p in preview["previews"]:
        if p["issue"]:
            parts.append("{}: BLOCKED - {}".format(p["target_param"], p["issue"]))
        else:
            line = "{}: {} -> {}".format(
                p["target_param"], _format_number(p["current_value"]), _format_number(p["new_value"])
            )
            if p.get("warning"):
                line += "  (WARNING: source is a TYPE parameter)"
            parts.append(line)
    # one mapping per line: the column wraps, so several mappings stay readable
    return "\n".join(parts)


class ParamMapRow(object):
    """One editable row in the parameter-mapping grid."""

    def __init__(self, source_param="", target_param="", multiplier="1"):
        self.SourceParam = source_param
        self.TargetParam = target_param
        self.Multiplier = multiplier


def _xaml_path():
    return os.path.join(os.path.dirname(__file__), "LoadTransfer.xaml")


def _set_items(control, values):
    control.ItemsSource = None
    control.ItemsSource = list(values)


def _is_checked(control_name):
    try:
        return bool(getattr(window, control_name).IsChecked)
    except Exception:
        return False


def _selected_family_key():
    try:
        return str(window.cmbDetailFamily.SelectedItem or "")
    except Exception:
        return ""


def _current_source_elements():
    return logic.collect_source_elements(
        doc,
        include_equipment=_is_checked("chkEquipment"),
        include_fixtures=_is_checked("chkFixtures"),
        fixture_types=_selected_fixture_types(),
        models=_selected_models()
    )


# ---------------------------------------------------------------------------
# Source models: this model and / or linked models
# ---------------------------------------------------------------------------

source_model_rows = []   # [(label, document, CheckBox)]


def _selected_models():
    """The ticked source documents ([] when nothing is ticked)."""
    out = []
    for _label, document, box in source_model_rows:
        try:
            if box.IsChecked and document.IsValidObject:
                out.append(document)
        except Exception:
            continue
    return out


def _selected_model_labels():
    return [label for label, _d, box in source_model_rows if box.IsChecked]


def _load_source_models(ticked=None):
    """(Re)build the source model list (needs Revit's API context). ticked: labels to tick (template);
    None keeps the current ticks - a model new in the list starts unticked, 'This model' ticked."""
    global source_model_rows
    from System.Windows.Controls import CheckBox

    before = dict((label, bool(box.IsChecked)) for label, _d, box in source_model_rows)
    rows = []
    window.lstSourceModels.Items.Clear()
    for label, document in logic.source_models(doc):
        box = CheckBox()
        box.Content = label if label == logic.HOST_MODEL_LABEL else u"Linked:  {}".format(label)
        if ticked is not None:
            box.IsChecked = label in ticked
        elif label in before:
            box.IsChecked = before[label]
        else:
            box.IsChecked = label == logic.HOST_MODEL_LABEL
        box.Click += _source_model_tick_changed
        window.lstSourceModels.Items.Add(box)
        rows.append((label, document, box))
    source_model_rows = rows
    _update_source_model_summary()


def _update_source_model_summary():
    ticked = _selected_model_labels()
    links = len(source_model_rows) - 1
    if not ticked:
        text = "No source model ticked - tick 'This model' and / or a linked model."
    else:
        text = "{} of {} model(s) ticked ({} loaded link(s) in this project).".format(
            len(ticked), len(source_model_rows), links)
    try:
        window.txtSourceModelsSummary.Text = text
    except Exception:
        pass


def _source_models_changed():
    """The source models changed: fixture types and parameter lists come from other models now."""
    _load_fixture_types(keep_ticks=True)
    _refresh_match_param_options()


def _source_model_tick_changed(sender, args):
    _update_source_model_summary()
    _run_in_revit("source models changed", _source_models_changed)
    if all_match_rows:
        _set_status("Source models changed -- press 'PREVIEW MATCHES' again so the matches use "
                    "the ticked models.")


# ---------------------------------------------------------------------------
# Electrical Fixture filter: tick which fixture family : types are sources
# ---------------------------------------------------------------------------

fixture_rows = []        # [(key, count, CheckBox)] in list order


def _selected_fixture_types():
    """None when every fixture type is ticked (no filter), else the set of ticked keys."""
    if not fixture_rows:
        return None
    ticked = set(key for key, _count, box in fixture_rows if box.IsChecked)
    if len(ticked) == len(fixture_rows):
        return None
    return ticked


def _load_fixture_types(keep_ticks=True, ticked=None):
    """(Re)build the fixture type list from the model (needs Revit's API context).

    keep_ticks: a type already in the list keeps its tick; a type that is NEW in the model is ticked
    only when every type was ticked before (no filter in use) -- a new type must not slip into a
    filtered transfer unnoticed. ticked: a set of keys to tick instead (template)."""
    global fixture_rows
    from System.Windows.Controls import CheckBox

    before = dict((key, bool(box.IsChecked)) for key, _c, box in fixture_rows) if keep_ticks else {}
    all_before = (not before) or all(before.values())
    groups = logic.collect_fixture_type_groups(doc, _selected_models())
    rows = []
    window.lstFixtureTypes.Items.Clear()
    for key in sorted(groups, key=lambda value: value.lower()):
        box = CheckBox()
        box.Content = u"{}   ({})".format(key, len(groups[key]))
        if ticked is not None:
            box.IsChecked = key in ticked
        elif key in before:
            box.IsChecked = before[key]
        else:
            box.IsChecked = all_before
        box.Click += _fixture_tick_changed
        window.lstFixtureTypes.Items.Add(box)
        rows.append((key, len(groups[key]), box))
    fixture_rows = rows
    _apply_fixture_search()
    _update_fixture_summary()


def _fixture_search_terms():
    try:
        return [term for term in (window.txtFixtureSearch.Text or "").lower().split() if term]
    except Exception:
        return []


def _apply_fixture_search():
    from System.Windows import Visibility
    terms = _fixture_search_terms()
    for key, _count, box in fixture_rows:
        show = all(term in key.lower() for term in terms)
        box.Visibility = Visibility.Visible if show else Visibility.Collapsed


def _update_fixture_summary():
    ticked = [(key, count) for key, count, box in fixture_rows if box.IsChecked]
    total_types = len(fixture_rows)
    if not total_types:
        text = "No Electrical Fixtures placed in the ticked source model(s)."
    elif len(ticked) == total_types:
        text = "All {} type(s) ticked - no filter ({} fixtures).".format(
            total_types, sum(count for _k, count, _b in fixture_rows))
    else:
        text = "{} of {} type(s) ticked - {} fixture(s) used.".format(
            len(ticked), total_types, sum(count for _k, count in ticked))
    try:
        window.txtFixtureSummary.Text = text
        window.brdFixtureFilter.IsEnabled = _is_checked("chkFixtures")
    except Exception:
        pass


def _fixture_filter_changed():
    """The source set changed: refresh the summary, the source parameter lists and stale matches."""
    _update_fixture_summary()
    _run_in_revit("fixture filter changed", _refresh_match_param_options)
    if all_match_rows:
        _set_status("Electrical Fixture filter changed -- press 'PREVIEW MATCHES' again so the "
                    "matches use only the ticked fixture types.")


def _fixture_tick_changed(sender, args):
    _fixture_filter_changed()


def _tick_shown_fixtures(value):
    from System.Windows import Visibility
    changed = False
    for _key, _count, box in fixture_rows:
        if box.Visibility == Visibility.Visible and bool(box.IsChecked) != value:
            box.IsChecked = value
            changed = True
    if changed:
        _fixture_filter_changed()


def btnFixturesAll_Click(sender, args):
    _tick_shown_fixtures(True)


def btnFixturesNone_Click(sender, args):
    _tick_shown_fixtures(False)


def txtFixtureSearch_TextChanged(sender, args):
    _apply_fixture_search()


def btnClearFixtureSearch_Click(sender, args):
    window.txtFixtureSearch.Text = ""
    _apply_fixture_search()


def _current_target_elements():
    key = _selected_family_key()
    return detail_groups.get(key, [])


def _refresh_match_param_options():
    """Repopulate the match-parameter dropdowns from the current
    source/target selections, preserving the previous choice if possible."""
    source_elements = _current_source_elements()
    target_elements = _current_target_elements()

    source_params = logic.common_parameter_names(source_elements)
    target_params = logic.common_parameter_names(target_elements)

    prev_source = None
    prev_target = None
    try:
        prev_source = window.cmbMatchSource.SelectedItem
        prev_target = window.cmbMatchTarget.SelectedItem
    except Exception:
        pass

    window.cmbMatchSource.ItemsSource = source_params
    window.cmbMatchTarget.ItemsSource = target_params

    if prev_source in source_params:
        window.cmbMatchSource.SelectedItem = prev_source
    elif "Mark" in source_params:
        window.cmbMatchSource.SelectedItem = "Mark"
    elif source_params:
        window.cmbMatchSource.SelectedIndex = 0

    if prev_target in target_params:
        window.cmbMatchTarget.SelectedItem = prev_target
    elif "Mark" in target_params:
        window.cmbMatchTarget.SelectedItem = "Mark"
    elif target_params:
        window.cmbMatchTarget.SelectedIndex = 0

    _refresh_mapping_pickers(source_params, target_params)


def _refresh_mapping_pickers(source_params, target_params):
    """Keep the 'Add Mapping' parameter pickers in sync with the same
    source/target parameter lists used for matching, preserving the
    current picks where possible. Deliberately does NOT fall back to
    selecting whatever happens to be first alphabetically when there's no
    previous selection -- that can land on an unrelated parameter (e.g. a
    Workset/district code) and risks silently adding a nonsense mapping if
    Add Mapping gets clicked without noticing. Better to leave it
    unselected and require a conscious pick."""
    prev_source = None
    prev_target = None
    try:
        prev_source = window.cmbPickSourceParam.SelectedItem
        prev_target = window.cmbPickTargetParam.SelectedItem
    except Exception:
        pass

    window.cmbPickSourceParam.ItemsSource = source_params
    window.cmbPickTargetParam.ItemsSource = target_params

    if prev_source in source_params:
        window.cmbPickSourceParam.SelectedItem = prev_source
    else:
        window.cmbPickSourceParam.SelectedIndex = -1

    if prev_target in target_params:
        window.cmbPickTargetParam.SelectedItem = prev_target
    else:
        window.cmbPickTargetParam.SelectedIndex = -1


def _detail_search_terms():
    try:
        query = (window.txtDetailSearch.Text or "").strip().lower()
    except Exception:
        return []
    return [term for term in query.split() if term]


def _apply_detail_search():
    """Repopulate cmbDetailFamily with the keys matching the search box.

    Every space-separated term has to appear somewhere in the key, in any
    order, so 'iso l03' finds 'S_EC_Isolator : L03-Type2' without the user
    having to remember whether the level or the family name comes first.

    The currently selected key is ALWAYS kept in the list even when it no
    longer matches. Dropping it would clear the selection, and the
    selection is what decides which elements get written to -- a filter
    typed into a search box must never silently change that. When it's
    only there because it was already selected, the summary line says so.
    """
    global suppress_detail_events

    terms = _detail_search_terms()
    selected = _selected_family_key()

    if terms:
        matches = [k for k in all_detail_keys
                   if all(term in k.lower() for term in terms)]
    else:
        matches = list(all_detail_keys)

    # Selection precedence, in order:
    #   1. the current selection, if it still matches  -- don't move it
    #   2. a single hit                                -- that's what the
    #      user narrowed to, and the combo shows it plainly
    #   3. otherwise keep the old selection, PINNED into the list even
    #      though it doesn't match. The selection decides which elements
    #      get written to, so typing in a search box must never silently
    #      clear it. Pinning last means it can't block case 2.
    keys = list(matches)
    pinned = False
    if selected and selected in matches:
        target = selected
    elif len(matches) == 1:
        target = matches[0]
    elif selected and selected in all_detail_keys:
        target = selected
        pinned = True
        keys.append(selected)
        keys.sort(key=lambda value: value.lower())
    elif matches and not terms:
        target = matches[0]
    else:
        target = None

    suppress_detail_events = True
    try:
        window.cmbDetailFamily.ItemsSource = keys
        if target is not None:
            window.cmbDetailFamily.SelectedItem = target
        else:
            window.cmbDetailFamily.SelectedIndex = -1
    finally:
        suppress_detail_events = False

    _update_detail_search_summary(matches, pinned)

    # SelectedItem may have moved while events were muted, so refresh the
    # dependent parameter pickers once, here, by hand.
    if _selected_family_key() != selected:
        _run_in_revit("detail search", _refresh_match_param_options)


def _update_detail_search_summary(matches, pinned=False):
    total = len(all_detail_keys)
    if not total:
        text = "No detail item families found in this model."
    elif not _detail_search_terms():
        text = "{} family : type combination(s) in the model.".format(total)
    else:
        text = "Showing {} of {} combination(s).".format(len(matches), total)
        if not matches:
            text += " No match -- clear the search or try fewer words."
        if pinned:
            text += (" Your current selection doesn't match the search but "
                     "is still listed and still selected.")
    try:
        window.txtDetailSearchSummary.Text = text
    except Exception:
        pass


def _load_detail_families():
    global detail_groups, all_detail_keys
    detail_groups = logic.collect_detail_item_groups(doc)
    all_detail_keys = sorted(detail_groups.keys(), key=lambda value: value.lower())
    _apply_detail_search()


def _set_status(text):
    try:
        window.txtStatus.Text = text
    except Exception:
        pass
    log(text)


# ---------------------------------------------------------------------------
# Log: %APPDATA%\EleTools\LoadTransfer\last_run.txt (one file per window session)
# ---------------------------------------------------------------------------

LOG_PATH = os.path.join(os.environ.get("APPDATA", ""), "EleTools", "LoadTransfer", "last_run.txt")


def log(text, new_session=False):
    try:
        import io
        import datetime
        folder = os.path.dirname(LOG_PATH)
        if not os.path.isdir(folder):
            os.makedirs(folder)
        with io.open(LOG_PATH, "w" if new_session else "a", encoding="utf-8") as stream:
            stream.write(u"{0}  {1}\n".format(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                              text if isinstance(text, unicode) else unicode(str(text), "utf-8", "replace")))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Modeless window: every Revit read / write runs through ONE External Event.
# A modeless window's clicks are outside Revit's API context, so a transaction started there
# fails (or silently does nothing). The click only queues the work; Revit runs it as soon as it
# is idle, inside its API context, on the same thread as this window (so the work can update
# the window directly and may open the modal review dialog).
# ---------------------------------------------------------------------------

_queue = []
_event = None
_handler = None


def _make_event():
    """Create the External Event. Must run while the button's script is executing (API context)."""
    global _event, _handler
    from Autodesk.Revit.UI import IExternalEventHandler, ExternalEvent

    class _RevitWork(IExternalEventHandler):
        def Execute(self, uiapp):
            work = list(_queue)
            del _queue[:]
            if not work:
                return
            problem = _document_problem(uiapp)
            if problem:
                log("not run ({}): {}".format(", ".join(label for label, _ in work), problem))
                forms.alert(problem, title="Load Transfer")
                return
            for label, action in work:
                try:
                    action()
                except Exception:
                    import traceback
                    detail = traceback.format_exc()
                    log(u"ERROR in {}:\n{}".format(label, detail))
                    forms.alert(u"{} failed - the error is written in\n{}\n\n{}".format(
                        label, LOG_PATH, detail[-700:]), title="Load Transfer")

        def GetName(self):
            return "Load Transfer"

    _handler = _RevitWork()
    _event = ExternalEvent.Create(_handler)


def _document_problem(uiapp):
    """Why the queued work must not run now (the model it was set up for is closed or not active)."""
    try:
        if doc is None or not doc.IsValidObject:
            return "The model this window was opened for has been closed. Close this window and start Load Transfer again."
        active = uiapp.ActiveUIDocument
        if active is None or not active.Document.Equals(doc):
            return u"Switch back to '{}' - this window works on that model.".format(doc.Title)
    except Exception:
        pass
    return None


def _run_in_revit(label, action):
    """Queue `action` (no arguments) for Revit. Without an External Event (modal fallback) it runs now."""
    if _event is None:
        action()
        return
    _queue.append((label, action))
    try:
        _event.Raise()
    except Exception as error:
        del _queue[:]
        log("could not raise the External Event: {}".format(error))
        forms.alert("Revit is busy - try again.", title="Load Transfer")


def _in_revit(handler, label=None):
    """Wrap a click handler so it runs inside Revit's API context."""
    name = label or handler.__name__

    def queued(sender, args):
        _run_in_revit(name, lambda: handler(sender, args))
    return queued


def _valid(element):
    try:
        return element is not None and element.IsValidObject
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Event handlers
# ---------------------------------------------------------------------------

def _source_toggle_changed(sender, args):
    _update_fixture_summary()               # greys the fixture filter out when Electrical Fixtures is off
    _refresh_match_param_options()


def cmbDetailFamily_SelectionChanged(sender, args):
    # Muted while _apply_detail_search() swaps ItemsSource. WPF raises
    # SelectionChanged during that swap, and letting it through would
    # rebuild the parameter pickers (and discard the user's picks) on
    # every keystroke in the search box. (Checked at click time, before
    # the work is queued for Revit.)
    if suppress_detail_events:
        return
    _run_in_revit("detail family changed", _refresh_match_param_options)


def txtDetailSearch_TextChanged(sender, args):
    _apply_detail_search()


def btnClearDetailSearch_Click(sender, args):
    """Reset the search. Re-filters directly rather than relying on the
    TextChanged event the assignment happens to raise -- the button must
    work whether or not that event fires."""
    window.txtDetailSearch.Text = ""
    _apply_detail_search()


def btnPreview_Click(sender, args):
    global current_matches, detail_groups

    # The window stays open while the model changes (modeless): read the detail items again,
    # so new ones are included and deleted ones never reach the transfer.
    detail_groups = logic.collect_detail_item_groups(doc)
    # ... the linked models (a link may have been loaded / unloaded meanwhile; ticks kept) ...
    _load_source_models()
    # ... and the fixture types (ticks kept; a new type is ticked only when no filter is in use)
    _load_fixture_types(keep_ticks=True)

    family_key = _selected_family_key()
    if not family_key:
        forms.alert("Choose a target Detail Item family first.", title="Load Transfer")
        return

    match_source_param = str(window.cmbMatchSource.SelectedItem or "")
    match_target_param = str(window.cmbMatchTarget.SelectedItem or "")
    if not match_source_param or not match_target_param:
        forms.alert("Choose a matching parameter for both source and target.", title="Load Transfer")
        return

    source_elements = _current_source_elements()
    target_elements = _current_target_elements()

    if not source_elements:
        if not _selected_models():
            forms.alert("No source model ticked: tick 'This model' and / or the linked model that "
                        "contains the electrical equipment / fixtures.", title="Load Transfer")
        elif _is_checked("chkFixtures") and _selected_fixture_types() is not None:
            forms.alert("No source elements: the Electrical Fixture filter has no fixture type ticked "
                        "(or the ticked types have no placed fixtures).", title="Load Transfer")
        else:
            forms.alert("No source elements found for the selected categories.", title="Load Transfer")
        return
    if not target_elements:
        forms.alert("No placed instances found for the selected Detail Item family.", title="Load Transfer")
        return

    current_matches = logic.build_matches(
        source_elements, target_elements, match_source_param, match_target_param
    )

    global all_match_rows
    all_match_rows = [MatchRow(match) for match in current_matches]

    # Re-apply the transfer filter to the fresh rows so the Include
    # column is never left showing stale ticks from a previous run.
    _apply_transfer_filter(announce=False)

    matched_count = sum(1 for m in current_matches if m["status"] == "matched")
    ambiguous_count = sum(1 for m in current_matches if m["status"] == "ambiguous")
    unmatched_count = len(current_matches) - matched_count - ambiguous_count
    included_count, _ = _include_counts()

    status_text = "{} matched, {} unmatched out of {} detail items.".format(
        matched_count, unmatched_count, len(current_matches)
    )
    if logic.parse_filter_keywords(_filter_text()):
        status_text += " Filter narrows this to {} item(s) for transfer.".format(included_count)
    if ambiguous_count:
        status_text += (
            " {} AMBIGUOUS (multiple sources share the same match value) -- "
            "these will be skipped. Check the grid and pick a more specific "
            "matching parameter, or make the match values unique."
        ).format(ambiguous_count)

    _set_status(status_text)

    if ambiguous_count:
        forms.alert(
            "{} target detail item(s) have more than one source element "
            "sharing the same match value -- these are flagged 'AMBIGUOUS' "
            "in the grid and will be skipped, not guessed at, when you "
            "transfer. Choose a more specific matching parameter (e.g. "
            "something unique per panel/circuit) if you want them included."
            .format(ambiguous_count),
            title="Ambiguous Matches Found"
        )


def _apply_match_search():
    """Filter the Match Preview grid by the search box (target name, match
    value, or status), so a specific element can be found without scrolling
    through hundreds of rows."""
    try:
        query = (window.txtMatchSearch.Text or "").strip().lower()
    except Exception:
        query = ""

    if not query:
        _set_items(window.dgMatches, all_match_rows)
        return

    filtered = [
        row for row in all_match_rows
        if query in row.TargetName.lower()
        or query in row.MatchValue.lower()
        or query in row.SourceName.lower()
        or query in row.Status.lower()
    ]
    _set_items(window.dgMatches, filtered)


def txtMatchSearch_TextChanged(sender, args):
    _apply_match_search()


# ---------------------------------------------------------------------------
# Transfer filter: which matched rows are actually allowed to be written
# ---------------------------------------------------------------------------

FILTER_FIELDS = ["Match Value", "Target Name", "Source Name", "Any of these"]

FILTER_MODE_LABELS = [
    ("Contains", "contains"),
    ("Starts with", "starts"),
    ("Ends with", "ends"),
    ("Exact match", "exact"),
]


def _selected_filter_field():
    try:
        return str(window.cmbFilterField.SelectedItem or FILTER_FIELDS[0])
    except Exception:
        return FILTER_FIELDS[0]


def _selected_filter_mode():
    try:
        label = str(window.cmbFilterMode.SelectedItem or "")
    except Exception:
        label = ""
    for text, mode in FILTER_MODE_LABELS:
        if text == label:
            return mode
    return "contains"


def _filter_text():
    try:
        return window.txtTransferFilter.Text or ""
    except Exception:
        return ""


def _include_counts():
    """(included, transferable) across every match row."""
    transferable = sum(1 for row in all_match_rows if row.CanTransfer)
    included = sum(1 for row in all_match_rows if row.CanTransfer and row.Include)
    return included, transferable


def _update_filter_summary():
    included, transferable = _include_counts()
    keywords = logic.parse_filter_keywords(_filter_text())

    if not all_match_rows:
        text = "Run Preview Matches to see what the filter will catch."
    elif not keywords:
        text = "No filter -- all {} matched item(s) will transfer.".format(transferable)
    else:
        text = "Filter [{}]: {} of {} matched item(s) will transfer.".format(
            ", ".join(keywords), included, transferable
        )
        if included == 0:
            text += (
                "  Nothing matches -- check the field/mode, or try 'Contains'."
            )
    try:
        window.txtFilterSummary.Text = text
    except Exception:
        pass
    return text


def _apply_transfer_filter(announce=True):
    """Re-tick the Include column from the current filter settings.

    Any manual tick/untick the user made is intentionally overwritten
    here -- this only runs when they explicitly apply the filter, hit
    Include All/None, or re-run Preview Matches, so it never surprises
    them mid-review.
    """
    keywords = logic.parse_filter_keywords(_filter_text())
    field = _selected_filter_field()
    mode = _selected_filter_mode()
    exclude = _is_checked("chkFilterExclude")

    for row in all_match_rows:
        if not row.CanTransfer:
            row.Include = False
            continue
        row.Include = logic.keyword_filter_decision(
            row.filter_values(field), keywords, mode, exclude
        )

    _apply_match_search()
    summary = _update_filter_summary()
    if announce:
        _set_status(summary)


def _set_all_included(value):
    for row in all_match_rows:
        row.Include = bool(value) and row.CanTransfer
    _apply_match_search()
    _set_status(_update_filter_summary())


def btnApplyFilter_Click(sender, args):
    if not all_match_rows:
        forms.alert("Click 'Preview Matches' first, then apply the filter.", title="Load Transfer")
        return
    _apply_transfer_filter()


def txtTransferFilter_TextChanged(sender, args):
    # Live-update the summary line as they type, but don't re-tick the
    # grid until Apply Filter is clicked -- retyping a keyword shouldn't
    # wipe manual Include choices on every keystroke.
    _update_filter_summary()


def chkFilterExclude_Changed(sender, args):
    _update_filter_summary()


def btnIncludeAll_Click(sender, args):
    _set_all_included(True)


def btnIncludeNone_Click(sender, args):
    _set_all_included(False)


def _included_matches():
    """The match dicts the user has actually approved for transfer."""
    return [
        row._match for row in all_match_rows
        if row.CanTransfer and row.Include
    ]


def btnSelectInModel_Click(sender, args):
    """Select and zoom to the exact source + target elements for the
    currently highlighted Match Preview row -- removes any doubt about
    which physical elements are actually involved in this match."""
    from Autodesk.Revit.DB import ElementId
    from System.Collections.Generic import List

    row = window.dgMatches.SelectedItem
    if row is None:
        forms.alert("Click a row in the Match Preview grid first, then click this button.", title="Load Transfer")
        return

    ids = []
    target = getattr(row, "_target_element", None)
    source = getattr(row, "_source_element", None)
    if target is not None:
        ids.append(target.Id)
    linked_source = source is not None and logic.model_name(source)
    if source is not None and not linked_source:
        ids.append(source.Id)

    if not ids and not linked_source:
        forms.alert("This row has no matched source to select.", title="Load Transfer")
        return

    if linked_source:
        # A linked element's Id belongs to the link, not to this model: select it through a link
        # reference (target + source together); fall back to the target only.
        if _select_with_linked_source(target, source):
            return
    if ids:
        id_list = List[ElementId](ids)
        uidoc.Selection.SetElementIds(id_list)
        uidoc.ShowElements(id_list)
    if linked_source:
        _set_status(u"Selected the detail item. Its source is in the linked model '{}' (Id {}).".format(
            linked_source, logic.id_number(source)))


def _select_with_linked_source(target, source):
    """Select the target detail item and the LINKED source element together (Revit 2023+:
    Selection.SetReferences with a link reference). False when it cannot be done."""
    from Autodesk.Revit.DB import FilteredElementCollector, Reference, RevitLinkInstance
    from System.Collections.Generic import List
    try:
        link = None
        for instance in FilteredElementCollector(doc).OfClass(RevitLinkInstance):
            link_doc = instance.GetLinkDocument()
            if link_doc is not None and link_doc.Equals(source.Document):
                link = instance
                break
        if link is None:
            return False
        refs = List[Reference]()
        if target is not None:
            refs.Add(Reference(target))
        refs.Add(Reference(source).CreateLinkReference(link))
        uidoc.Selection.SetReferences(refs)
        if target is not None:
            from Autodesk.Revit.DB import ElementId
            uidoc.ShowElements(List[ElementId]([target.Id]))
        _set_status(u"Selected the detail item and its source in the linked model '{}'.".format(
            logic.model_name(source)))
        return True
    except Exception as error:
        log(u"select linked source failed: {}".format(error))
        return False


def _select_or_inject(combo, value):
    """Select `value` in a picker, adding it to the list if the current
    model doesn't offer it.

    A mapping loaded from a template can name a parameter that isn't in
    the dropdown yet (no Preview Matches run, or a different family
    selected). Without this, SelectedItem silently stays null, the picker
    shows blank, and pressing Update would quietly blank out a mapping
    the user only meant to re-multiply.
    """
    if not value:
        combo.SelectedIndex = -1
        return
    items = list(combo.ItemsSource or [])
    if value not in items:
        items.append(value)
        items.sort()
        combo.ItemsSource = items
    combo.SelectedItem = value


def _set_edit_mode(row):
    """Enter (row given) or leave (None) mapping-edit mode."""
    global editing_row
    editing_row = row
    if row is None:
        window.btnAddMapping.Content = "Add Mapping"
        window.btnEditMapRow.Content = "Edit Selected"
    else:
        window.btnAddMapping.Content = "Update Mapping"
        window.btnEditMapRow.Content = "Cancel Edit"


def _validate_mapping(source_param, target_param, multiplier_text, ignore_row=None):
    """Shared gate for Add and Update. Returns an error string, or None
    if the mapping is acceptable. ignore_row is the row being edited, so
    a row doesn't collide with its own unchanged self.
    """
    if not source_param or not target_param:
        return "Pick both a Source Parameter and a Target Parameter first."

    try:
        float(multiplier_text)
    except ValueError:
        return "Multiplier must be a number, e.g. 1 or 0.001."

    for row in param_map_rows:
        if row is ignore_row:
            continue
        if row.SourceParam == source_param and row.TargetParam == target_param:
            return "'{}' -> '{}' is already in the list below.".format(
                source_param, target_param
            )
    return None


def _confirm_type_parameter(target_param):
    """True if the mapping should proceed. Warns (once, with a yes/no)
    when the target is TYPE-bound."""
    target_elements = _current_target_elements()
    if not target_elements:
        return True
    if logic.parameter_scope(target_elements[0], target_param) != "type":
        return True
    return bool(forms.alert(
        "'{}' is a TYPE parameter on this family -- every placed "
        "instance of this type SHARES one value for it. Writing "
        "different per-instance loads into it means each write "
        "overwrites the previous one for every instance of that "
        "type, and they'll all end up showing whichever value was "
        "written last.\n\n"
        "Fix this in the Family Editor first: open the family, "
        "Family Types, change '{}' from Type to Instance, then "
        "reload it into the project.\n\n"
        "Use this mapping anyway?".format(target_param, target_param),
        yes=True,
        no=True,
        title="Type Parameter Warning"
    ))


def _refresh_map_grid(select=None):
    _set_items(window.dgParamMap, param_map_rows)
    if select is not None and select in param_map_rows:
        window.dgParamMap.SelectedItem = select
        try:
            window.dgParamMap.ScrollIntoView(select)
        except Exception:
            pass


def btnAddMapping_Click(sender, args):
    """Adds a new mapping, or commits an edit to the row currently loaded
    into the pickers."""
    source_param = str(window.cmbPickSourceParam.SelectedItem or "").strip()
    target_param = str(window.cmbPickTargetParam.SelectedItem or "").strip()
    multiplier_text = (window.txtPickMultiplier.Text or "1").strip()

    error = _validate_mapping(source_param, target_param, multiplier_text,
                              ignore_row=editing_row)
    if error:
        forms.alert(error, title="Load Transfer")
        return

    # Only re-warn when the target parameter is actually new to this row.
    needs_scope_check = editing_row is None or editing_row.TargetParam != target_param
    if needs_scope_check and not _confirm_type_parameter(target_param):
        return

    if editing_row is None:
        row = ParamMapRow(source_param, target_param, multiplier_text)
        param_map_rows.append(row)
    else:
        row = editing_row
        row.SourceParam = source_param
        row.TargetParam = target_param
        row.Multiplier = multiplier_text
        _set_edit_mode(None)

    _refresh_map_grid(select=row)
    _invalidate_value_preview()


def btnEditMapRow_Click(sender, args):
    """Toggle: loads the selected row into the pickers, or cancels an
    edit already in progress."""
    if editing_row is not None:
        _set_edit_mode(None)
        return

    selected = list(window.dgParamMap.SelectedItems or [])
    if not selected:
        forms.alert("Select the mapping row you want to edit first.",
                    title="Load Transfer")
        return
    if len(selected) > 1:
        forms.alert("Select a single row to edit. (Remove Selected can "
                    "take more than one.)", title="Load Transfer")
        return

    row = selected[0]
    _select_or_inject(window.cmbPickSourceParam, row.SourceParam)
    _select_or_inject(window.cmbPickTargetParam, row.TargetParam)
    window.txtPickMultiplier.Text = str(row.Multiplier)
    _set_edit_mode(row)


def dgParamMap_MouseDoubleClick(sender, args):
    if window.dgParamMap.SelectedItem is None:
        return
    try:
        # The Multiplier column is edited in place; a double-click there
        # is the user starting a cell edit, not asking to load the row
        # into the pickers.
        column = window.dgParamMap.CurrentColumn
        if column is not None and column.DisplayIndex == 2:
            return
    except Exception:
        pass
    if editing_row is not None:
        _set_edit_mode(None)
    btnEditMapRow_Click(sender, args)


def btnRemoveMapRow_Click(sender, args):
    global param_map_rows

    selected = list(window.dgParamMap.SelectedItems or [])
    if not selected:
        # Used to return silently, which reads as a dead button.
        forms.alert("Select the mapping row(s) you want to remove first. "
                    "Ctrl+click or Shift+click to select more than one.",
                    title="Load Transfer")
        return

    if len(selected) > 1:
        proceed = forms.alert(
            "Remove {} mapping row(s)?".format(len(selected)),
            yes=True, no=True, title="Remove Mappings"
        )
        if not proceed:
            return

    if editing_row in selected:
        _set_edit_mode(None)

    param_map_rows = [row for row in param_map_rows if row not in selected]
    _refresh_map_grid()
    _invalidate_value_preview()


def btnClearMapRows_Click(sender, args):
    global param_map_rows

    if not param_map_rows:
        return
    proceed = forms.alert(
        "Remove all {} mapping row(s)?".format(len(param_map_rows)),
        yes=True, no=True, title="Clear Mappings"
    )
    if not proceed:
        return

    _set_edit_mode(None)
    param_map_rows = []
    _refresh_map_grid()
    _invalidate_value_preview()


def _invalidate_value_preview():
    """The Value Preview column reflects the mapping list at the moment it
    was generated. Changing the mapping afterwards leaves stale text in
    the match grid that no longer describes what would be written, so it
    gets cleared and the user is told to re-run Preview Values.
    """
    if not all_match_rows:
        return
    stale = False
    for row in all_match_rows:
        if getattr(row, "ValuePreview", ""):
            row.ValuePreview = ""
            stale = True
    if stale:
        _apply_match_search()
        _set_status("Mapping changed -- press 'Preview Values' again to "
                    "refresh the Value Preview column.")


def _commit_param_map_edit():
    try:
        window.dgParamMap.CommitEdit()
        window.dgParamMap.CommitEdit()
    except Exception:
        pass


def _collect_param_pairs():
    """Parse the mapping grid into (source_param, target_param, multiplier)
    tuples. Returns (pairs, error_message). error_message is None on
    success; on failure pairs is None and error_message explains why."""
    _commit_param_map_edit()
    pairs = []
    for row in param_map_rows:
        if not row.SourceParam or not row.TargetParam:
            continue
        try:
            multiplier = float(row.Multiplier) if str(row.Multiplier).strip() else 1.0
        except ValueError:
            return None, "Multiplier for '{}' -> '{}' isn't a valid number: '{}'".format(
                row.SourceParam, row.TargetParam, row.Multiplier
            )
        pairs.append((row.SourceParam.strip(), row.TargetParam.strip(), multiplier))

    if not pairs:
        return None, "Add at least one Source Parameter -> Target Parameter mapping."
    return pairs, None


def btnPreviewValues_Click(sender, args):
    global all_match_rows

    if not current_matches:
        forms.alert("Click 'Preview Matches' first.", title="Load Transfer")
        return

    param_pairs, error = _collect_param_pairs()
    if error:
        forms.alert(error, title="Load Transfer")
        return

    preview_results = logic.preview_transfer(current_matches, param_pairs)

    # Update the existing rows rather than rebuilding them, so manual
    # Include ticks survive.
    for row, preview in zip(all_match_rows, preview_results):
        row.set_preview(preview)
    _apply_match_search()

    # Only count problems on rows that are actually going to be written
    # -- a blocked write on a row the filter already excluded isn't a
    # problem, and flagging it would send the user hunting for a
    # non-issue.
    blocked = 0
    warned = 0
    for row, preview in zip(all_match_rows, preview_results):
        if not (row.CanTransfer and row.Include):
            continue
        for p in preview["previews"]:
            if p["issue"]:
                blocked += 1
            if p.get("warning"):
                warned += 1

    included_count, transferable = _include_counts()
    scope = "Value Preview ready -- {} of {} matched item(s) included".format(
        included_count, transferable
    )

    if blocked:
        message = (
            "{} -- {} field write(s) on INCLUDED rows would be BLOCKED. "
            "Check the 'Value Preview' column before transferring."
            .format(scope, blocked)
        )
    elif warned:
        message = (
            "{} -- {} included row(s) read from a TYPE-bound source parameter "
            "(see WARNING in Value Preview) -- that source value may be a "
            "shared spec/rating, not unique per installation."
            .format(scope, warned)
        )
    else:
        message = (
            "{} -- check the 'Value Preview' column, then TRANSFER LOADS if it "
            "looks right.".format(scope)
        )

    _set_status(message)
    _update_filter_summary()


def btnReviewChanges_Click(sender, args):
    """Open the before/after table without offering to write anything --
    for checking the numbers, or exporting them, before committing."""
    if not current_matches:
        forms.alert("Click 'Preview Matches' first.", title="Load Transfer")
        return

    param_pairs, error = _collect_param_pairs()
    if error:
        forms.alert(error, title="Load Transfer")
        return

    matches_to_review = _included_matches()
    if not matches_to_review:
        forms.alert(
            "Every matched item is currently excluded, so there's nothing to "
            "review.\n\nClear the Transfer Filter, or tick rows in the "
            "'Include' column.",
            title="Load Transfer"
        )
        return

    report_rows = logic.build_transfer_report(matches_to_review, param_pairs)
    if not report_rows:
        forms.alert(
            "Nothing to review -- the mapped source parameters have no values "
            "on the matched elements.",
            title="Load Transfer"
        )
        return

    totals = logic.report_totals(report_rows)
    preview.show(report_rows, allow_confirm=False, owner=window)
    _set_status(
        "Reviewed: {} value(s) would change across {} detail item(s), "
        "{} blocked, {} already correct.".format(
            totals["writes"], totals["elements"], totals["blocked"], totals["unchanged"]
        )
    )


def btnTransfer_Click(sender, args):
    if not current_matches:
        forms.alert("Click 'Preview Matches' before transferring.", title="Load Transfer")
        return

    param_pairs, error = _collect_param_pairs()
    if error:
        forms.alert(error, title="Load Transfer")
        return

    # The model may have changed since Preview Matches (the window is modeless).
    gone = [m for m in current_matches if m["matched"] and not (_valid(m["target"]) and _valid(m["source"]))]
    if gone:
        forms.alert(
            "{} matched element(s) were deleted since 'Preview Matches'. Run Preview Matches "
            "again before transferring.".format(len(gone)),
            title="Load Transfer"
        )
        return

    matched_count = sum(1 for match in current_matches if match["matched"])
    if matched_count == 0:
        forms.alert("No unambiguously matched pairs to transfer.", title="Load Transfer")
        return

    # Only rows ticked Include are written. Everything from here on works
    # off this list, so the count in the confirmation dialog is exactly
    # what gets transferred.
    matches_to_transfer = _included_matches()
    included_count = len(matches_to_transfer)
    excluded_count = matched_count - included_count

    if included_count == 0:
        forms.alert(
            "Every matched item is currently excluded, so there's nothing to "
            "transfer.\n\nClear the Transfer Filter, or tick rows in the "
            "'Include' column of the Match Preview grid.",
            title="Load Transfer"
        )
        return

    sample_target = matches_to_transfer[0]["target"]

    if sample_target is not None:
        type_bound = [
            tgt for _, tgt, _ in param_pairs
            if logic.parameter_scope(sample_target, tgt) == "type"
        ]
        if type_bound:
            proceed = forms.alert(
                "These target parameter(s) are TYPE-bound (shared across "
                "every instance of this Detail Item type): {}.\n\n"
                "Writing per-instance values into them would make every "
                "instance of that type show whichever value is written last, "
                "so they are SKIPPED (shown BLOCKED in the review). Change "
                "them to Instance parameters in the Family Editor to "
                "transfer them.\n\n"
                "Continue with the other parameters?".format(", ".join(type_bound)),
                yes=True,
                no=True,
                title="Type Parameter Warning"
            )
            if not proceed:
                return

    # Build the before/after table from the SAME filtered match list the
    # transfer will use, so the review can't show one thing and write
    # another.
    report_rows = logic.build_transfer_report(matches_to_transfer, param_pairs)
    if not report_rows:
        forms.alert(
            "Nothing to preview -- the mapped source parameters have no "
            "values on the matched elements.",
            title="Load Transfer"
        )
        return

    context_note = ""
    if excluded_count:
        keywords = logic.parse_filter_keywords(_filter_text())
        if keywords:
            context_note = (
                "{} further matched item(s) are excluded by the filter [{}] "
                "and are not listed here".format(excluded_count, ", ".join(keywords))
            )
        else:
            context_note = (
                "{} further matched item(s) are unticked in the Include "
                "column and are not listed here".format(excluded_count)
            )

    confirmed = preview.show(report_rows, allow_confirm=True, context_note=context_note, owner=window)
    if not confirmed:
        _set_status("Transfer cancelled from the review window -- nothing was written.")
        return

    result = logic.transfer(doc, matches_to_transfer, param_pairs)

    summary = "Updated: {} detail item(s), {} value(s) written.".format(result["updated"], result.get("values", 0))
    if result.get("unchanged"):
        summary += "\nAlready correct (not rewritten): {}".format(result["unchanged"])
    if result.get("skipped"):
        summary += "\nBlocked and skipped: {}".format(result["skipped"])
    if result["errors"]:
        summary += "\nIssues:\n" + "\n".join(result["errors"][:20])
        if len(result["errors"]) > 20:
            summary += "\n... and {} more".format(len(result["errors"]) - 20)

    status_line = "Transfer complete. Updated {} detail item(s).".format(result["updated"])
    if excluded_count:
        status_line += " {} matched item(s) were excluded and left untouched.".format(excluded_count)
        summary += "\n\nExcluded (not touched): {}".format(excluded_count)
    _set_status(status_line)
    forms.alert(summary, title="Load Transfer Complete")


# ---------------------------------------------------------------------------
# Templates: save/load the whole setup for reuse
# ---------------------------------------------------------------------------

def _refresh_template_list(select_name=None):
    names = templates.available_template_names()
    window.cmbTemplates.ItemsSource = names
    if select_name and select_name in names:
        window.cmbTemplates.SelectedItem = select_name
    elif names:
        window.cmbTemplates.SelectedIndex = 0
    else:
        window.cmbTemplates.SelectedIndex = -1


def _selected_template_name():
    try:
        return str(window.cmbTemplates.SelectedItem or "")
    except Exception:
        return ""


def _current_config_snapshot():
    _commit_param_map_edit()
    return {
        "include_equipment": _is_checked("chkEquipment"),
        "include_fixtures": _is_checked("chkFixtures"),
        # the ticked source models by name ('This model' and / or linked model titles)
        "source_models": _selected_model_labels(),
        # None = every fixture type (no filter); else the ticked 'Family : Type' keys
        "fixture_types": (sorted(_selected_fixture_types()) if _selected_fixture_types() is not None else None),
        "target_family_key": _selected_family_key(),
        "match_source_param": str(window.cmbMatchSource.SelectedItem or ""),
        "match_target_param": str(window.cmbMatchTarget.SelectedItem or ""),
        "filter_text": _filter_text(),
        "filter_field": _selected_filter_field(),
        "filter_mode": str(getattr(window.cmbFilterMode, "SelectedItem", "") or ""),
        "filter_exclude": _is_checked("chkFilterExclude"),
        "mappings": [
            {"source": row.SourceParam, "target": row.TargetParam, "multiplier": row.Multiplier}
            for row in param_map_rows
        ]
    }


def _apply_config_snapshot(config):
    global param_map_rows

    window.chkEquipment.IsChecked = bool(config.get("include_equipment", True))
    window.chkFixtures.IsChecked = bool(config.get("include_fixtures", True))
    # Source models (older templates have no key -> this model only, as before)
    saved_models = config.get("source_models")
    _load_source_models(ticked=set(saved_models) if saved_models else set([logic.HOST_MODEL_LABEL]))
    if saved_models:
        missing = [label for label in saved_models if label not in [l for l, _d, _b in source_model_rows]]
        if missing:
            log(u"template source models not loaded in this project: {}".format(u", ".join(missing)))
            _set_status(u"Template: linked model(s) not loaded here: {}".format(u", ".join(missing)))
    # Electrical Fixture filter (older templates have no key -> every type ticked, as before)
    saved_types = config.get("fixture_types")
    _load_fixture_types(keep_ticks=False, ticked=set(saved_types) if saved_types is not None else None)
    if saved_types:
        missing = [key for key in saved_types if key not in [k for k, _c, _b in fixture_rows]]
        if missing:
            log(u"template fixture types not in this model: {}".format(u", ".join(missing)))

    family_key = config.get("target_family_key", "")
    if family_key and family_key in detail_groups:
        # A template's family may be filtered out of the combo by whatever
        # is currently typed in the search box. Clear the search rather
        # than fail to restore the template's target.
        if family_key not in list(window.cmbDetailFamily.ItemsSource or []):
            window.txtDetailSearch.Text = ""
            _apply_detail_search()
        window.cmbDetailFamily.SelectedItem = family_key

    _refresh_match_param_options()

    match_source = config.get("match_source_param", "")
    match_target = config.get("match_target_param", "")
    try:
        if match_source in list(window.cmbMatchSource.ItemsSource or []):
            window.cmbMatchSource.SelectedItem = match_source
        if match_target in list(window.cmbMatchTarget.ItemsSource or []):
            window.cmbMatchTarget.SelectedItem = match_target
    except Exception:
        pass

    # Filter settings. Templates saved before the filter existed simply
    # have none of these keys, so they fall back to "no filter" and keep
    # behaving exactly as they did.
    try:
        window.txtTransferFilter.Text = config.get("filter_text", "") or ""
        window.chkFilterExclude.IsChecked = bool(config.get("filter_exclude", False))

        field = config.get("filter_field", "")
        if field in FILTER_FIELDS:
            window.cmbFilterField.SelectedItem = field
        else:
            window.cmbFilterField.SelectedIndex = 0

        mode_label = config.get("filter_mode", "")
        mode_labels = [text for text, _ in FILTER_MODE_LABELS]
        if mode_label in mode_labels:
            window.cmbFilterMode.SelectedItem = mode_label
        else:
            window.cmbFilterMode.SelectedIndex = 0
    except Exception:
        pass

    # A template swaps the whole list out. Any in-progress edit points at
    # a row that no longer exists, so drop it rather than let Update write
    # into an orphan.
    _set_edit_mode(None)
    param_map_rows = [
        ParamMapRow(m.get("source", ""), m.get("target", ""), m.get("multiplier", "1"))
        for m in config.get("mappings", [])
    ]
    _set_items(window.dgParamMap, param_map_rows)
    _update_filter_summary()


def btnSaveTemplate_Click(sender, args):
    name = forms.ask_for_string(
        default="My Template",
        prompt="Enter a name for this template",
        title="Save Template"
    )
    if not name:
        return

    existing = templates.available_template_names()
    if name in existing:
        replace = forms.alert(
            "A template named '{}' already exists. Replace it?".format(name),
            yes=True,
            no=True,
            title="Replace Template"
        )
        if not replace:
            return

    try:
        templates.save_template(name, _current_config_snapshot())
    except Exception as error:
        forms.alert(str(error), title="Save Template Error")
        return

    _refresh_template_list(select_name=name)
    forms.alert("Template saved: {}".format(name), title="Load Transfer")


def btnLoadTemplate_Click(sender, args):
    name = _selected_template_name()
    if not name:
        forms.alert("Choose a saved template first.", title="Load Transfer")
        return

    try:
        config = templates.load_template(name)
    except Exception as error:
        forms.alert(str(error), title="Load Template Error")
        return

    _apply_config_snapshot(config)

    # If a match list is already on screen, re-apply the template's
    # filter to it immediately rather than leaving the Include column
    # showing the previous template's decisions.
    if all_match_rows:
        _apply_transfer_filter(announce=False)

    _set_status("Template '{}' loaded. Run Preview Matches to continue.".format(name))


def btnDeleteTemplate_Click(sender, args):
    name = _selected_template_name()
    if not name:
        forms.alert("Choose a saved template first.", title="Load Transfer")
        return

    confirmed = forms.alert(
        "Delete the saved template '{}'? This cannot be undone.".format(name),
        yes=True,
        no=True,
        title="Delete Template"
    )
    if not confirmed:
        return

    templates.delete_template(name)
    _refresh_template_list()


def fit_to_screen(win, share=0.92):
    """Never open taller / wider than the screen's working area (laptops at 125-150 % scaling
    have only ~800 px of height) - a window taller than the screen hides its lower part."""
    try:
        from System.Windows import SystemParameters
        area = SystemParameters.WorkArea
        win.MinHeight = min(win.MinHeight, area.Height * 0.8)
        win.MinWidth = min(win.MinWidth, area.Width * 0.8)
        win.Height = min(win.Height, area.Height * share)
        win.Width = min(win.Width, area.Width * share)
    except Exception:
        pass


def bind_events():
    # Handlers that READ or WRITE the model are wrapped in _in_revit (run through the External
    # Event); handlers that only change the window run directly.
    window.btnLoadTemplate.Click += _in_revit(btnLoadTemplate_Click, "Load template")
    window.btnSaveTemplate.Click += btnSaveTemplate_Click
    window.btnDeleteTemplate.Click += btnDeleteTemplate_Click

    source_changed = _in_revit(_source_toggle_changed, "source categories changed")
    window.chkEquipment.Checked += source_changed
    window.chkEquipment.Unchecked += source_changed
    window.chkFixtures.Checked += source_changed
    window.chkFixtures.Unchecked += source_changed
    window.cmbDetailFamily.SelectionChanged += cmbDetailFamily_SelectionChanged
    window.txtDetailSearch.TextChanged += txtDetailSearch_TextChanged
    window.btnClearDetailSearch.Click += btnClearDetailSearch_Click
    window.btnPreview.Click += _in_revit(btnPreview_Click, "Preview Matches")

    # Electrical Fixture filter (ticks and search only change the window; the parameter lists are
    # refreshed through the External Event inside _fixture_filter_changed)
    window.txtFixtureSearch.TextChanged += txtFixtureSearch_TextChanged
    window.btnClearFixtureSearch.Click += btnClearFixtureSearch_Click
    window.btnFixturesAll.Click += btnFixturesAll_Click
    window.btnFixturesNone.Click += btnFixturesNone_Click

    window.btnApplyFilter.Click += btnApplyFilter_Click
    window.txtTransferFilter.TextChanged += txtTransferFilter_TextChanged
    window.chkFilterExclude.Checked += chkFilterExclude_Changed
    window.chkFilterExclude.Unchecked += chkFilterExclude_Changed
    window.btnIncludeAll.Click += btnIncludeAll_Click
    window.btnIncludeNone.Click += btnIncludeNone_Click

    window.txtMatchSearch.TextChanged += txtMatchSearch_TextChanged
    window.btnSelectInModel.Click += _in_revit(btnSelectInModel_Click, "Select in Model")
    window.btnAddMapping.Click += _in_revit(btnAddMapping_Click, "Add Mapping")
    window.btnPreviewValues.Click += _in_revit(btnPreviewValues_Click, "Preview Values")
    window.btnReviewChanges.Click += _in_revit(btnReviewChanges_Click, "Review Old vs New")
    window.btnRemoveMapRow.Click += btnRemoveMapRow_Click
    window.btnEditMapRow.Click += btnEditMapRow_Click
    window.btnClearMapRows.Click += btnClearMapRows_Click
    window.dgParamMap.MouseDoubleClick += dgParamMap_MouseDoubleClick
    window.btnTransfer.Click += _in_revit(btnTransfer_Click, "Transfer Loads")


def run():
    global window, doc, uidoc, param_map_rows, current_matches, all_match_rows, editing_row, source_model_rows

    # One window at a time: pressing the button again brings the open one to the front.
    try:
        if window is not None and window.IsVisible:
            if doc is not None and doc.IsValidObject and revit.doc is not None and revit.doc.Equals(doc):
                window.Activate()
                return
            window.Close()
    except Exception:
        pass

    doc = revit.doc
    uidoc = revit.uidoc
    current_matches, all_match_rows, editing_row = [], [], None
    source_model_rows = []                     # documents of the previous project are not reused
    del _queue[:]
    log(u"Load Transfer opened on '{}'".format(doc.Title if doc is not None else "?"), new_session=True)
    _make_event()                          # must happen now, while the button's script runs
    window = forms.WPFWindow(_xaml_path())
    fit_to_screen(window)
    window.Title = u"Load Transfer  -  {}".format(doc.Title)

    templates.configure()

    # A sensible starter row -- edit/remove/add freely in the UI.
    # 0.001 converts Watts (Elec Apparent Power) to kW (a typical TCL field).
    param_map_rows = [
        ParamMapRow("Elec Apparent Power", "TCL", "0.001"),
    ]
    _set_items(window.dgParamMap, param_map_rows)

    window.cmbFilterField.ItemsSource = FILTER_FIELDS
    window.cmbFilterField.SelectedIndex = 0
    window.cmbFilterMode.ItemsSource = [text for text, _ in FILTER_MODE_LABELS]
    window.cmbFilterMode.SelectedIndex = 0

    bind_events()
    _load_detail_families()
    _load_source_models()                      # 'This model' ticked, linked models listed unticked
    _load_fixture_types(keep_ticks=False)      # every fixture type ticked = no filter
    _refresh_match_param_options()
    _refresh_template_list()
    _update_filter_summary()

    # MODELESS: Revit stays usable while the window is open. Every click that reads or writes the
    # model goes through the External Event (see _in_revit), so writes run inside Revit's API
    # context - the reason the window used to be modal. script.py sets __persistentengine__ so the
    # window's code stays alive after the button's script has finished.
    window.Closed += lambda sender, args: log("Load Transfer closed")
    window.show()
