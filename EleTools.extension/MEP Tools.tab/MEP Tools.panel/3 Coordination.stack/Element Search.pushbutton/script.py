# -*- coding: utf-8 -*-
"""Search MEP elements by any parameter value, through a WPF interface."""
__title__ = "MEP\nSearch"
__author__ = 'BHEEM'
__doc__ = ("Search Revit MEP elements (Electrical Equipment, Electrical Fixtures, "
           "Lighting, Mechanical Equipment, Ducts, Pipes, Cable Trays, Conduits, "
           "Plumbing Fixtures, etc.) by any instance or type parameter value. "
           "Select, zoom to, isolate, or export matching elements.")

import clr
clr.AddReference('RevitAPI')
clr.AddReference('RevitAPIUI')
clr.AddReference('PresentationFramework')
clr.AddReference('PresentationCore')
clr.AddReference('WindowsBase')

import json

import System
from System.Collections.Generic import List as NetList
from System.Collections.ObjectModel import ObservableCollection

from Autodesk.Revit.DB import (
    FilteredElementCollector, BuiltInCategory, Category,
    Element, ElementId, StorageType, Transaction, TemporaryViewMode,
    TextNote
)
from Autodesk.Revit.UI import IExternalEventHandler, ExternalEvent

# Text Notes store their body text as a dedicated .Text property on the
# TextNote object, not as a normal Parameter, so it never shows up via
# LookupParameter(). We expose it as this synthetic "parameter" name.
TEXT_CONTENT_PARAM = "Text Content (Note Body)"

from pyrevit import revit, forms, script

logger = script.get_logger()

# This window is shown modeless (non-modal) so it can stay open while the
# user keeps working in Revit. That means Revit is NOT in a valid API
# context while the window is open: every doc/uidoc/Element call from a
# button click has to be marshalled back into Revit through an
# ExternalEvent, or it silently fails. __persistentengine__ keeps this
# script's engine alive so queued calls keep working for as long as the
# window stays open.
__persistentengine__ = True

doc = revit.doc
uidoc = revit.uidoc

# ---------------------------------------------------------------------
# Friendly category name -> BuiltInCategory, covering the common MEP
# disciplines: Electrical, Mechanical, Plumbing/Piping, and Fire Protection.
# ---------------------------------------------------------------------
MEP_CATEGORIES = {
    "Electrical Equipment": BuiltInCategory.OST_ElectricalEquipment,
    "Electrical Fixtures": BuiltInCategory.OST_ElectricalFixtures,
    "Lighting Fixtures": BuiltInCategory.OST_LightingFixtures,
    "Lighting Devices": BuiltInCategory.OST_LightingDevices,
    "Mechanical Equipment": BuiltInCategory.OST_MechanicalEquipment,
    "Air Terminals": BuiltInCategory.OST_DuctTerminal,
    "Ducts": BuiltInCategory.OST_DuctCurves,
    "Duct Fittings": BuiltInCategory.OST_DuctFitting,
    "Duct Accessories": BuiltInCategory.OST_DuctAccessory,
    "Duct Insulations": BuiltInCategory.OST_DuctInsulations,
    "Flex Ducts": BuiltInCategory.OST_FlexDuctCurves,
    "Pipes": BuiltInCategory.OST_PipeCurves,
    "Pipe Fittings": BuiltInCategory.OST_PipeFitting,
    "Pipe Accessories": BuiltInCategory.OST_PipeAccessory,
    "Pipe Insulations": BuiltInCategory.OST_PipeInsulations,
    "Flex Pipes": BuiltInCategory.OST_FlexPipeCurves,
    "Plumbing Fixtures": BuiltInCategory.OST_PlumbingFixtures,
    "Sprinklers": BuiltInCategory.OST_Sprinklers,
    "Fire Alarm Devices": BuiltInCategory.OST_FireAlarmDevices,
    "Fire Protection": BuiltInCategory.OST_FireProtection,
    "Communication Devices": BuiltInCategory.OST_CommunicationDevices,
    "Data Devices": BuiltInCategory.OST_DataDevices,
    "Security Devices": BuiltInCategory.OST_SecurityDevices,
    "Nurse Call Devices": BuiltInCategory.OST_NurseCallDevices,
    "Telephone Devices": BuiltInCategory.OST_TelephoneDevices,
    "Cable Trays": BuiltInCategory.OST_CableTray,
    "Cable Tray Fittings": BuiltInCategory.OST_CableTrayFitting,
    "Conduits": BuiltInCategory.OST_Conduit,
    "Conduit Fittings": BuiltInCategory.OST_ConduitFitting,
    "Detail Items": BuiltInCategory.OST_DetailComponents,
    "Text Notes": BuiltInCategory.OST_TextNotes,
}

OPERATORS = [
    "Equals", "Contains", "Starts With", "Ends With",
    "Greater Than", "Less Than", "Not Equals",
    "Has Any Value", "Is Empty",
]



def _id_int(element_id):
    """ElementId -> int in every Revit version: .Value (2024+) - .IntegerValue was REMOVED in Revit 2026."""
    value = getattr(element_id, 'Value', None)
    return int(element_id.IntegerValue if value is None else value)


def get_param_value_as_string(param):
    """Return a human readable string for any parameter's current value."""
    if param is None or not param.HasValue:
        return ""
    try:
        if param.StorageType == StorageType.String:
            return param.AsString() or ""
        elif param.StorageType == StorageType.Integer:
            value_string = param.AsValueString()
            return value_string if value_string else str(param.AsInteger())
        elif param.StorageType == StorageType.Double:
            value_string = param.AsValueString()
            return value_string if value_string else str(param.AsDouble())
        elif param.StorageType == StorageType.ElementId:
            eid = param.AsElementId()
            if eid is not None and _id_int(eid) > -1:
                el = doc.GetElement(eid)
                try:
                    return Element.Name.__get__(el) if el else str(_id_int(eid))
                except Exception:
                    return el.Name if el else str(eid.IntegerValue)
            return ""
    except Exception:
        try:
            return param.AsValueString() or ""
        except Exception:
            return ""
    return ""


def get_param_numeric(param):
    """Return a float for numeric-storage parameters, else None."""
    try:
        if param.StorageType == StorageType.Double:
            return param.AsDouble()
        elif param.StorageType == StorageType.Integer:
            return float(param.AsInteger())
    except Exception:
        pass
    return None


def collect_elements(bic):
    return list(
        FilteredElementCollector(doc)
        .OfCategory(bic)
        .WhereElementIsNotElementType()
        .ToElements()
    )


def get_type_element(el):
    try:
        type_id = el.GetTypeId()
        if type_id and _id_int(type_id) > -1:
            return doc.GetElement(type_id)
    except Exception:
        pass
    return None


def get_all_param_names(elements, sample_size=75):
    """Union of instance + type parameter names across a sample of elements,
    so the parameter dropdown reflects what's actually on these elements."""
    names = set()
    has_text_note = False
    for i, el in enumerate(elements):
        if i >= sample_size:
            break
        if isinstance(el, TextNote):
            has_text_note = True
        for p in el.Parameters:
            if p.Definition:
                names.add(p.Definition.Name)
        etype = get_type_element(el)
        if etype:
            for p in etype.Parameters:
                if p.Definition:
                    names.add(p.Definition.Name)
    if has_text_note:
        names.add(TEXT_CONTENT_PARAM)
    return sorted(names)


def find_param(el, param_name):
    param = el.LookupParameter(param_name)
    if param is None:
        etype = get_type_element(el)
        if etype:
            param = etype.LookupParameter(param_name)
    return param


def get_search_value(el, param_name):
    """Return (found, value_string, param_or_None) for a given element and
    parameter/field name. Handles Text Note body content specially since
    it isn't a real Parameter."""
    if isinstance(el, TextNote) and param_name == TEXT_CONTENT_PARAM:
        try:
            text_value = el.Text or ""
        except Exception:
            text_value = ""
        return True, text_value, None

    param = find_param(el, param_name)
    if param is None:
        return False, "", None
    return True, get_param_value_as_string(param), param


class ResultRow(object):
    """Row shown in the results grid; also carries the ElementId for actions."""

    def __init__(self, element, param_name, value):
        self.ElementId = _id_int(element.Id)
        self.Category = element.Category.Name if element.Category else ""

        self.Family = ""
        try:
            self.Family = element.Symbol.Family.Name
        except Exception:
            try:
                self.Family = element.get_Symbol().Family.Name
            except Exception:
                self.Family = ""

        self.TypeName = ""
        try:
            etype = get_type_element(element)
            self.TypeName = etype.Name if etype else element.Name
        except Exception:
            self.TypeName = ""

        self.Level = ""
        try:
            level_param = element.LookupParameter("Reference Level") or element.LookupParameter("Level")
            if level_param:
                self.Level = get_param_value_as_string(level_param)
        except Exception:
            pass

        self.ParamName = param_name
        self.Value = value


class GenericEventHandler(IExternalEventHandler):
    """One reusable ExternalEvent handler. Any button that needs to touch
    doc/uidoc/an Element hands this a no-arg callable; Execute() runs it
    once Revit raises the event back into a valid API context, on the
    main thread, so it's safe to touch WPF controls from inside the
    callable too."""
    def __init__(self):
        self.action = None
        self._ensure_globals = _ensure_globals

    def Execute(self, uiapp):
        self._ensure_globals()
        action, self.action = self.action, None
        if action is None:
            return
        try:
            action()
        except Exception as ex:
            try:
                forms.alert("Error: {}".format(ex), title="MEP Search", warn_icon=True)
            except Exception:
                pass

    def GetName(self):
        return "MEP Search - Generic Handler"


# ---------------------------------------------------------------------------
# Self-healing global namespace repair (same pattern used in the Cross
# Check Panels tool). In this pyRevit environment, methods on a modeless
# window can lose access to the script's own module-level names once the
# window has been open a while - a plain bare-name lookup that always
# works in normal Python can raise NameError here. _MODULE_SNAPSHOT
# captures everything defined in this module once, at load time (when
# things are known-good); _ensure_globals() re-injects anything missing
# back into this module's namespace. Called from every button handler.
# ---------------------------------------------------------------------------
_MODULE_SNAPSHOT = dict(globals())


def _ensure_globals(_snapshot=_MODULE_SNAPSHOT):
    # _snapshot is a default-argument value, bound directly onto this
    # function object at definition time - unlike a bare global lookup,
    # it survives even if this module's globals() dict gets wiped later.
    globals().update(_snapshot)


class SearchWindow(forms.WPFWindow):
    def __init__(self, xaml_file):
        forms.WPFWindow.__init__(self, xaml_file)

        self._ensure_globals = _ensure_globals
        self._ensure_globals()

        self.event_handler = GenericEventHandler()
        self.ext_event = ExternalEvent.Create(self.event_handler)
        self._suppress_category_event = False

        self.results = []
        self._current_elements = []
        self.config = script.get_config()
        self._populate_categories()
        self.cmbOperator.ItemsSource = OPERATORS
        self.cmbOperator.SelectedIndex = 0
        self.lblStatus.Text = "Select a category to begin."
        self._populate_template_list()
        self._restore_on_open()

    # -- API-context helper -------------------------------------------------
    def run_in_api_context(self, func):
        """Queue func (a no-arg callable) to run through the ExternalEvent,
        i.e. inside Revit's valid API context. Required for any doc/uidoc/
        Element access while this modeless window is open."""
        self.event_handler.action = func
        self.ext_event.Raise()

    # -------------------------------------------------------------
    def _cfg_get(self, name, default):
        try:
            return self.config.get_option(name, default)
        except Exception:
            return getattr(self.config, name, default)

    # ---- named templates ------------------------------------------------
    def _load_templates(self):
        raw = self._cfg_get('templates_json', '')
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except Exception:
            return {}

    def _save_templates(self, templates):
        try:
            self.config.templates_json = json.dumps(templates)
            script.save_config()
        except Exception as ex:
            logger.debug("Could not save templates: {0}".format(ex))

    def _populate_template_list(self):
        templates = self._load_templates()
        self.cmbTemplates.ItemsSource = sorted(templates.keys())
        return templates

    def _apply_template(self, tmpl):
        """Apply a saved template dict to the form controls, including the
        doc-touching category selection. Must be called from a context
        that's already inside the Revit API (either during __init__, or
        from inside a run_in_api_context-queued callable) since it selects
        a category and collects elements directly. Returns True if the
        category was found and applied."""
        category_name = tmpl.get('category', '')
        parameter_name = tmpl.get('parameter', '')
        operator_ = tmpl.get('operator', 'Equals')
        value = tmpl.get('value', '')
        case_sensitive = tmpl.get('case_sensitive', False)

        self.chkCaseSensitive.IsChecked = bool(case_sensitive)
        self.txtValue.Text = value or ""

        match = None
        for item in (self.cmbCategory.ItemsSource or []):
            if item.split("  (")[0] == category_name:
                match = item
                break
        if match:
            # Setting SelectedItem fires cmbCategory_change synchronously.
            # We suppress its normal (async, ExternalEvent-queued) behavior
            # here and instead run the category-selection logic directly,
            # since we're already inside a valid API context.
            self._suppress_category_event = True
            try:
                self.cmbCategory.SelectedItem = match
            finally:
                self._suppress_category_event = False
            self._select_category_by_name(category_name)

        if parameter_name and self.cmbParameter.ItemsSource:
            found_param = False
            for p in self.cmbParameter.ItemsSource:
                if p == parameter_name:
                    self.cmbParameter.SelectedItem = p
                    found_param = True
                    break
            if not found_param:
                self.cmbParameter.Text = parameter_name

        if operator_ in OPERATORS:
            self.cmbOperator.SelectedItem = operator_

        return bool(match)

    def btnSaveTemplate_click(self, sender, args):
        self._ensure_globals()
        cat_selected = self.cmbCategory.SelectedItem
        if not cat_selected:
            forms.alert("Choose a category before saving a template.")
            return
        param_name = self.cmbParameter.Text or self.cmbParameter.SelectedItem
        if not param_name:
            forms.alert("Choose a parameter before saving a template.")
            return

        cat_name = cat_selected.split("  (")[0]
        operator_ = self.cmbOperator.SelectedItem or "Equals"
        search_val = self.txtValue.Text or ""
        case_sensitive = bool(self.chkCaseSensitive.IsChecked)

        default_name = "{0} - {1}".format(cat_name, param_name)
        template_name = forms.ask_for_string(
            default=default_name,
            prompt="Name this search template:",
            title="Save Search Template")
        if not template_name:
            return

        templates = self._load_templates()
        templates[template_name] = {
            "category": cat_name,
            "parameter": param_name,
            "operator": operator_,
            "value": search_val,
            "case_sensitive": case_sensitive,
        }
        self._save_templates(templates)
        self._remember_last_template(template_name)
        self._populate_template_list()
        self.cmbTemplates.SelectedItem = template_name
        self.lblStatus.Text = "Saved template '{0}'.".format(template_name)

    def btnLoadTemplate_click(self, sender, args):
        self._ensure_globals()
        name = self.cmbTemplates.SelectedItem
        if not name:
            forms.alert("Pick a saved template first.")
            return
        templates = self._load_templates()
        tmpl = templates.get(name)
        if not tmpl:
            forms.alert("That template could not be found.")
            return
        self.run_in_api_context(lambda: self._do_load_template(name, tmpl))

    def _do_load_template(self, name, tmpl):
        applied = self._apply_template(tmpl)
        self._remember_last_template(name)
        self.lblStatus.Text = ("Loaded template '{0}'.".format(name) if applied else
                                "Loaded template '{0}', but its category is not present in this model.".format(name))

    def btnLoadLastTemplate_click(self, sender, args):
        self._ensure_globals()
        last_name = self._cfg_get('last_template_name', '')
        templates = self._load_templates()
        if not last_name or last_name not in templates:
            forms.alert("No saved template yet. Use 'Save As Template...' first.")
            return
        self.run_in_api_context(lambda: self._do_load_last_template(last_name, templates[last_name]))

    def _do_load_last_template(self, last_name, tmpl):
        applied = self._apply_template(tmpl)
        self.cmbTemplates.SelectedItem = last_name
        self.lblStatus.Text = ("Imported last saved template '{0}'.".format(last_name) if applied else
                                "Imported template '{0}', but its category is not present in this model.".format(last_name))

    def btnDeleteTemplate_click(self, sender, args):
        self._ensure_globals()
        name = self.cmbTemplates.SelectedItem
        if not name:
            forms.alert("Pick a saved template first.")
            return
        if not forms.alert("Delete template '{0}'?".format(name), yes=True, no=True):
            return
        templates = self._load_templates()
        if name in templates:
            del templates[name]
            self._save_templates(templates)
        if self._cfg_get('last_template_name', '') == name:
            self._remember_last_template("")
        self._populate_template_list()
        self.lblStatus.Text = "Deleted template '{0}'.".format(name)

    def _remember_last_template(self, name):
        try:
            self.config.last_template_name = name
            script.save_config()
        except Exception as ex:
            logger.debug("Could not remember last template: {0}".format(ex))

    def _restore_on_open(self):
        """Prefer the last saved template; fall back to raw last-used values."""
        last_name = self._cfg_get('last_template_name', '')
        templates = self._load_templates()
        if last_name and last_name in templates:
            applied = self._apply_template(templates[last_name])
            if applied:
                self.cmbTemplates.SelectedItem = last_name
                self.lblStatus.Text += "  |  Restored last saved template '{0}'.".format(last_name)
                return
        self._restore_last_search()

    # -------------------------------------------------------------
    def _restore_last_search(self):
        saved_category = self._cfg_get('last_category', '')
        saved_parameter = self._cfg_get('last_parameter', '')
        saved_operator = self._cfg_get('last_operator', 'Equals')
        saved_value = self._cfg_get('last_value', '')
        saved_case = self._cfg_get('last_case_sensitive', False)

        if not saved_category:
            return

        self.chkCaseSensitive.IsChecked = bool(saved_case)
        self.txtValue.Text = saved_value or ""

        match = None
        for item in (self.cmbCategory.ItemsSource or []):
            if item.split("  (")[0] == saved_category:
                match = item
                break
        if match:
            # We're inside __init__ here (valid API context), so run the
            # category-selection logic directly rather than through the
            # async ExternalEvent path cmbCategory_change normally uses.
            self._suppress_category_event = True
            try:
                self.cmbCategory.SelectedItem = match
            finally:
                self._suppress_category_event = False
            self._select_category_by_name(saved_category)

        if saved_parameter and self.cmbParameter.ItemsSource:
            found_param = False
            for p in self.cmbParameter.ItemsSource:
                if p == saved_parameter:
                    self.cmbParameter.SelectedItem = p
                    found_param = True
                    break
            if not found_param:
                # Parameter wasn't in the sampled list (editable combo box).
                self.cmbParameter.Text = saved_parameter

        if saved_operator in OPERATORS:
            self.cmbOperator.SelectedItem = saved_operator

        if match:
            self.lblStatus.Text += "  |  Restored last search settings."

    def _save_last_search(self, category_name, param_name, operator_, search_val, case_sensitive):
        try:
            self.config.last_category = category_name
            self.config.last_parameter = param_name
            self.config.last_operator = operator_
            self.config.last_value = search_val
            self.config.last_case_sensitive = case_sensitive
            script.save_config()
        except Exception as ex:
            logger.debug("Could not save search settings: {0}".format(ex))

    def btnForgetSettings_click(self, sender, args):
        self._ensure_globals()
        self._save_last_search("", "", "Equals", "", False)
        self.chkCaseSensitive.IsChecked = False
        self.cmbOperator.SelectedIndex = 0
        self.txtValue.Text = ""
        self.lblStatus.Text = "Saved search settings cleared."

    # -------------------------------------------------------------
    def _populate_categories(self):
        available = []
        for name in sorted(MEP_CATEGORIES.keys()):
            bic = MEP_CATEGORIES[name]
            try:
                cat = Category.GetCategory(doc, bic)
            except Exception:
                cat = None
            if cat is None:
                continue
            count = (
                FilteredElementCollector(doc)
                .OfCategory(bic)
                .WhereElementIsNotElementType()
                .GetElementCount()
            )
            available.append("{0}  ({1})".format(name, count))
        self.cmbCategory.ItemsSource = available

    # -------------------------------------------------------------
    def cmbCategory_change(self, sender, args):
        self._ensure_globals()
        if self._suppress_category_event:
            return
        selected = self.cmbCategory.SelectedItem
        if not selected:
            return
        cat_name = selected.split("  (")[0]
        self.run_in_api_context(lambda: self._select_category_by_name(cat_name))

    def _select_category_by_name(self, cat_name):
        """The actual doc-touching work of selecting a category. Called
        either via cmbCategory_change (queued through ExternalEvent, for
        real user interaction) or directly from _apply_template/
        _restore_last_search when we're already inside a valid API
        context (during __init__, or already inside a queued callable)."""
        bic = MEP_CATEGORIES.get(cat_name)
        if bic is None:
            return
        elements = collect_elements(bic)
        self._current_elements = elements
        param_names = get_all_param_names(elements)
        self.cmbParameter.ItemsSource = param_names
        self.dgResults.ItemsSource = None
        self.results = []
        self.lblStatus.Text = "{0} element(s) in '{1}'. {2} parameter(s) available.".format(
            len(elements), cat_name, len(param_names))

    # -------------------------------------------------------------
    def btnSearch_click(self, sender, args):
        self._ensure_globals()
        if not self._current_elements:
            forms.alert("Choose a category first.")
            return

        param_name = self.cmbParameter.Text or self.cmbParameter.SelectedItem
        if not param_name:
            forms.alert("Choose or type a parameter name first.")
            return

        operator_ = self.cmbOperator.SelectedItem or "Equals"
        search_val = (self.txtValue.Text or "").strip()
        case_sensitive = bool(self.chkCaseSensitive.IsChecked)

        if operator_ not in ("Has Any Value", "Is Empty") and not search_val:
            forms.alert("Enter a value to search for.")
            return

        self.run_in_api_context(
            lambda: self._do_search(param_name, operator_, search_val, case_sensitive))

    def _do_search(self, param_name, operator_, search_val, case_sensitive):
        matches = []
        for el in self._current_elements:
            found, val_str, param = get_search_value(el, param_name)
            if not found:
                continue

            cmp_val = val_str if case_sensitive else val_str.lower()
            cmp_search = search_val if case_sensitive else search_val.lower()
            has_value = (param.HasValue if param is not None else True) and val_str != ""

            is_match = False
            if operator_ == "Has Any Value":
                is_match = has_value
            elif operator_ == "Is Empty":
                is_match = not has_value
            elif operator_ == "Equals":
                is_match = cmp_val == cmp_search
            elif operator_ == "Not Equals":
                is_match = cmp_val != cmp_search
            elif operator_ == "Contains":
                is_match = cmp_search in cmp_val
            elif operator_ == "Starts With":
                is_match = cmp_val.startswith(cmp_search)
            elif operator_ == "Ends With":
                is_match = cmp_val.endswith(cmp_search)
            elif operator_ in ("Greater Than", "Less Than"):
                num_val = get_param_numeric(param) if param is not None else None
                try:
                    num_search = float(search_val)
                except ValueError:
                    num_search = None
                if num_val is not None and num_search is not None:
                    is_match = (num_val > num_search) if operator_ == "Greater Than" else (num_val < num_search)

            if is_match:
                matches.append(ResultRow(el, param_name, val_str))

        self.results = matches
        self.dgResults.ItemsSource = ObservableCollection[object](matches)
        self.lblStatus.Text = "{0} of {1} element(s) matched '{2}' {3} '{4}'.".format(
            len(matches), len(self._current_elements), param_name, operator_, search_val)

        cat_selected = self.cmbCategory.SelectedItem
        cat_name = cat_selected.split("  (")[0] if cat_selected else ""
        self._save_last_search(cat_name, param_name, operator_, search_val, case_sensitive)

    # -------------------------------------------------------------
    def _selected_element_ids(self):
        rows = list(self.dgResults.SelectedItems)
        if not rows:
            rows = self.results
        return [ElementId(r.ElementId) for r in rows]

    def btnSelect_click(self, sender, args):
        self._ensure_globals()
        ids = self._selected_element_ids()
        if not ids:
            forms.alert("No results to select.")
            return
        self.run_in_api_context(lambda: self._do_select(ids))

    def _do_select(self, ids):
        uidoc.Selection.SetElementIds(NetList[ElementId](ids))
        self.lblStatus.Text = "Selected {0} element(s) in the model.".format(len(ids))

    def btnShow_click(self, sender, args):
        self._ensure_globals()
        ids = self._selected_element_ids()
        if not ids:
            forms.alert("No results to show.")
            return
        self.run_in_api_context(lambda: self._do_show(ids))

    def _do_show(self, ids):
        id_collection = NetList[ElementId](ids)
        uidoc.Selection.SetElementIds(id_collection)
        uidoc.ShowElements(id_collection)
        self.lblStatus.Text = "Zoomed to {0} element(s).".format(len(ids))

    def btnIsolate_click(self, sender, args):
        self._ensure_globals()
        ids = self._selected_element_ids()
        if not ids:
            forms.alert("No results to isolate.")
            return
        self.run_in_api_context(lambda: self._do_isolate(ids))

    def _do_isolate(self, ids):
        view = doc.ActiveView
        t = Transaction(doc, "MEP Search - Isolate Elements")
        t.Start()
        try:
            id_collection = NetList[ElementId](ids)
            view.IsolateElementsTemporary(id_collection)
            t.Commit()
            self.lblStatus.Text = "Isolated {0} element(s) in the active view.".format(len(ids))
        except Exception as ex:
            t.RollBack()
            forms.alert("Could not isolate elements:\n{0}".format(ex))

    def btnClearIsolate_click(self, sender, args):
        self._ensure_globals()
        self.run_in_api_context(self._do_clear_isolate)

    def _do_clear_isolate(self):
        view = doc.ActiveView
        t = Transaction(doc, "MEP Search - Clear Isolate")
        t.Start()
        try:
            if view.IsTemporaryHideIsolateActive():
                view.DisableTemporaryViewMode(TemporaryViewMode.TemporaryHideIsolate)
            t.Commit()
            self.lblStatus.Text = "Temporary isolate cleared."
        except Exception as ex:
            t.RollBack()
            forms.alert("Could not clear isolate:\n{0}".format(ex))

    def btnExport_click(self, sender, args):
        self._ensure_globals()
        if not self.results:
            forms.alert("No results to export.")
            return
        save_path = forms.save_file(file_ext='csv')
        if not save_path:
            return
        with open(save_path, 'w') as f:
            f.write("ElementId,Category,Family,Type,Level,Parameter,Value\n")
            for r in self.results:
                row = [str(r.ElementId), r.Category, r.Family, r.TypeName, r.Level, r.ParamName, r.Value]
                row = [c.replace(',', ';') for c in row]
                f.write(",".join(row) + "\n")
        forms.alert("Exported {0} row(s) to:\n{1}".format(len(self.results), save_path))

    def btnClear_click(self, sender, args):
        self._ensure_globals()
        self.txtValue.Text = ""
        self.dgResults.ItemsSource = None
        self.results = []
        self.lblStatus.Text = "Results cleared."


if __name__ == '__main__':
    SearchWindow('ui.xaml').show(modal=False)
