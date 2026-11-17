# -*- coding: utf-8 -*-
"""Excel <-> Revit MEP data exchange.

Reads and writes .xlsx directly through the Open XML package, so Microsoft
Excel does not need to be installed. Works against every MEP discipline
(HVAC, piping, plumbing, electrical, fire protection, fabrication) plus
generic models and detail items.

Revit 2019 - 2026 compatible: all version-sensitive API calls are probed at
runtime rather than assumed.
"""
import clr
import json
import os
import re

clr.AddReference('System.Windows.Forms')
clr.AddReference('System.IO.Compression')
# .NET Framework (Revit <= 2024) keeps ZipFile in System.IO.Compression.FileSystem;
# .NET 8 (Revit 2025+) keeps it in System.IO.Compression.ZipFile and only a
# type-forwarding facade under the old name, which IronPython cannot import
# from. Reference whichever exists.
for _zip_assembly in ('System.IO.Compression.ZipFile', 'System.IO.Compression.FileSystem'):
    try:
        clr.AddReference(_zip_assembly)
    except Exception:
        pass
clr.AddReference('System.Xml')
clr.AddReference('PresentationFramework')
clr.AddReference('PresentationCore')
clr.AddReference('WindowsBase')

from System import DateTime, Int64
from System.Windows.Forms import (OpenFileDialog, SaveFileDialog, DialogResult,
                                  FolderBrowserDialog)
from System.IO import (StreamReader, StreamWriter, File, Path, FileStream, FileMode,
                       FileAccess, FileShare)
from System.IO.Compression import ZipFile, ZipArchive, ZipArchiveMode, CompressionLevel
from System.Xml import XmlDocument
from System.Text import Encoding, UTF8Encoding
from System.Windows import Thickness, Visibility
from System.Windows.Controls import ListBoxItem, CheckBox, TextBlock
from System.Windows.Input import Cursors
from System.Windows.Media import SolidColorBrush, Color
from System.Collections.Generic import List

from Autodesk.Revit.DB import (BuiltInCategory, Category, Element, ElementId,
                               ElementCategoryFilter, ElementMulticategoryFilter,
                               FilteredElementCollector, LogicalOrFilter,
                               StorageType, UnitUtils, ViewSchedule)
from pyrevit import forms, revit, script

TITLE = 'Excel <-> Revit MEP'

# --------------------------------------------------------------------------
# Optional API surface. Each of these moved or appeared in a different Revit
# release, so probe rather than import at module scope and hard-fail.
# --------------------------------------------------------------------------
try:
    from Autodesk.Revit.DB import SpecTypeId
except ImportError:
    SpecTypeId = None
try:
    from Autodesk.Revit.DB import ParameterType
except ImportError:
    ParameterType = None
try:
    from Autodesk.Revit.DB import Level, Material, Phase, ElementType, MEPSystemType
except ImportError:
    Level = Material = Phase = ElementType = MEPSystemType = None
try:
    from Autodesk.Revit.DB import (WorksharingUtils, CheckoutStatus, ModelUpdatesStatus)
except ImportError:
    WorksharingUtils = CheckoutStatus = ModelUpdatesStatus = None

doc = revit.doc
output = script.get_output()

if doc is None or doc.IsFamilyDocument:
    forms.alert('Open a project document. This tool does not run in the Family Editor.',
                title='Excel <-> Revit MEP', exitscript=True)

MATCH_BRUSH = SolidColorBrush(Color.FromRgb(0, 150, 70))        # bright green
NO_MATCH_BRUSH = SolidColorBrush(Color.FromRgb(229, 57, 53))     # bright red
INFO_BRUSH = SolidColorBrush(Color.FromRgb(61, 90, 254))         # indigo (headings / counts)

PREVIEW_LIMIT = 750          # rows drawn in the preview list before truncating
TYPE_SAMPLE_LIMIT = 3000     # distinct element types scanned when listing parameters

xlsx_path = None
xlsx_sheets = []             # [(sheet name, zip entry)] in workbook order
shared_strings = []
date_styles = set()
columns = []                 # [(display, header, index)]
sheet_data = []
all_elements = []
last_results = []
schedule_field_ids = {}
family_cache = {}
name_lookup_cache = None


# ==========================================================================
# Version-safe primitives
# ==========================================================================
def id_value(element_id):
    """ElementId.IntegerValue was deprecated in 2024 and removed in 2026.
    ElementId.Value (Int64) replaces it. Probe, do not assume."""
    if element_id is None:
        return -1
    try:
        return element_id.Value
    except AttributeError:
        return element_id.IntegerValue


ELEMENT_ID_IS_64BIT = hasattr(ElementId.InvalidElementId, 'Value')


def make_element_id(number):
    # Pick the constructor explicitly: on Revit <= 2023 there is no Int64
    # overload, and letting IronPython guess between (int), (BuiltInCategory)
    # and (BuiltInParameter) is not something to rely on.
    if ELEMENT_ID_IS_64BIT:
        return ElementId(Int64(number))
    return ElementId(int(number))


def element_type(element):
    try:
        type_id = element.GetTypeId()
        if type_id is None or type_id == ElementId.InvalidElementId:
            return None
        return doc.GetElement(type_id)
    except Exception:
        return None


def family(element):
    """Family name, cached per element type. Handles loadable families and
    system families (duct, pipe, conduit, cable tray, wire)."""
    try:
        key = id_value(element.GetTypeId())
    except Exception:
        return '<Unknown Family>'
    if key in family_cache:
        return family_cache[key]
    name = None
    symbol = element_type(element)
    if symbol is not None:
        try:
            name = symbol.FamilyName
        except Exception:
            name = None
        if not name:
            try:
                name = Element.Name.GetValue(symbol)
            except Exception:
                name = None
    family_cache[key] = name or '<Unknown Family>'
    return family_cache[key]


def type_name(element):
    symbol = element_type(element)
    if symbol is None:
        return ''
    try:
        return Element.Name.GetValue(symbol) or ''
    except Exception:
        return ''


def category_name(element):
    try:
        return element.Category.Name if element.Category else ''
    except Exception:
        return ''


# ==========================================================================
# WPF helpers
# ==========================================================================
def checkbox_item(text, checked, handler=None):
    item = CheckBox()
    # Shown through a TextBlock: with a plain string WPF swallows the first
    # "_" of a name (schedule, family, parameter) as a shortcut-key marker.
    # The real text is kept in Tag for checked_labels / filter_listbox.
    shown = TextBlock()
    shown.Text = text
    item.Content = shown
    item.Tag = text
    item.IsChecked = checked
    item.Margin = Thickness(2)
    if handler:
        item.Checked += handler
        item.Unchecked += handler
    return item


def checked_labels(listbox):
    labels = set()
    for item in listbox.Items:
        try:
            if item.IsChecked:
                labels.add(str(item.Tag))
        except AttributeError:
            pass
    return labels


def filter_listbox(listbox, text):
    text = (text or '').strip().lower()
    for item in listbox.Items:
        try:
            label = str(item.Tag).lower()
        except AttributeError:
            continue
        item.Visibility = Visibility.Visible if (not text or text in label) else Visibility.Collapsed


def check_all(listbox):
    for item in listbox.Items:
        if item.Visibility != Visibility.Collapsed:
            item.IsChecked = True


def check_none(listbox):
    for item in listbox.Items:
        if item.Visibility != Visibility.Collapsed:
            item.IsChecked = False


def busy(active):
    try:
        window.Cursor = Cursors.Wait if active else Cursors.Arrow
    except Exception:
        pass


# ==========================================================================
# Category catalogue - every MEP discipline
# ==========================================================================
CATEGORY_CANDIDATES = [
    # HVAC
    ('HVAC', 'Air Terminals', 'OST_DuctTerminal'),
    ('HVAC', 'Ducts', 'OST_DuctCurves'),
    ('HVAC', 'Duct Fittings', 'OST_DuctFitting'),
    ('HVAC', 'Duct Accessories', 'OST_DuctAccessory'),
    ('HVAC', 'Duct Insulations', 'OST_DuctInsulations'),
    ('HVAC', 'Duct Linings', 'OST_DuctLinings'),
    ('HVAC', 'Duct Placeholders', 'OST_PlaceHolderDucts'),
    ('HVAC', 'Duct Systems', 'OST_DuctSystem'),
    ('HVAC', 'Flex Ducts', 'OST_FlexDuctCurves'),
    ('HVAC', 'Mechanical Equipment', 'OST_MechanicalEquipment'),
    ('HVAC', 'Mechanical Equipment Sets', 'OST_MechanicalEquipmentSet'),
    ('HVAC', 'Mechanical Control Devices', 'OST_MechanicalControlDevices'),
    ('HVAC', 'HVAC Zones', 'OST_HVAC_Zones'),
    ('HVAC', 'Spaces', 'OST_MEPSpaces'),
    # Piping and plumbing
    ('Piping', 'Pipes', 'OST_PipeCurves'),
    ('Piping', 'Pipe Fittings', 'OST_PipeFitting'),
    ('Piping', 'Pipe Accessories', 'OST_PipeAccessory'),
    ('Piping', 'Pipe Insulations', 'OST_PipeInsulations'),
    ('Piping', 'Pipe Placeholders', 'OST_PlaceHolderPipes'),
    ('Piping', 'Piping Systems', 'OST_PipingSystem'),
    ('Piping', 'Flex Pipes', 'OST_FlexPipeCurves'),
    ('Piping', 'Plumbing Fixtures', 'OST_PlumbingFixtures'),
    ('Piping', 'Plumbing Equipment', 'OST_PlumbingEquipment'),
    # Fire protection
    ('Fire Protection', 'Sprinklers', 'OST_Sprinklers'),
    ('Fire Protection', 'Fire Protection', 'OST_FireProtection'),
    ('Fire Protection', 'Fire Alarm Devices', 'OST_FireAlarmDevices'),
    # Electrical
    ('Electrical', 'Electrical Equipment', 'OST_ElectricalEquipment'),
    ('Electrical', 'Electrical Fixtures', 'OST_ElectricalFixtures'),
    ('Electrical', 'Electrical Circuits', 'OST_ElectricalCircuit'),
    ('Electrical', 'Lighting Fixtures', 'OST_LightingFixtures'),
    ('Electrical', 'Lighting Devices', 'OST_LightingDevices'),
    ('Electrical', 'Conduits', 'OST_Conduit'),
    ('Electrical', 'Conduit Fittings', 'OST_ConduitFitting'),
    ('Electrical', 'Conduit Runs', 'OST_ConduitRun'),
    ('Electrical', 'Cable Trays', 'OST_CableTray'),
    ('Electrical', 'Cable Tray Fittings', 'OST_CableTrayFitting'),
    ('Electrical', 'Cable Tray Runs', 'OST_CableTrayRun'),
    ('Electrical', 'Wires', 'OST_Wire'),
    ('Electrical', 'Communication Devices', 'OST_CommunicationDevices'),
    ('Electrical', 'Data Devices', 'OST_DataDevices'),
    ('Electrical', 'Nurse Call Devices', 'OST_NurseCallDevices'),
    ('Electrical', 'Security Devices', 'OST_SecurityDevices'),
    ('Electrical', 'Telephone Devices', 'OST_TelephoneDevices'),
    # Fabrication
    ('Fabrication', 'Fabrication Ductwork', 'OST_FabricationDuctwork'),
    ('Fabrication', 'Fabrication Pipework', 'OST_FabricationPipework'),
    ('Fabrication', 'Fabrication Hangers', 'OST_FabricationHangers'),
    ('Fabrication', 'Fabrication Containment', 'OST_FabricationContainment'),
    ('Fabrication', 'Fabrication Duct Insulation', 'OST_FabricationDuctworkInsulation'),
    ('Fabrication', 'Fabrication Pipe Insulation', 'OST_FabricationPipeworkInsulation'),
    # Shared hosts
    ('General', 'Generic Models', 'OST_GenericModel'),
    ('General', 'Detail Items', 'OST_DetailComponents'),
    ('General', 'Specialty Equipment', 'OST_SpecialityEquipment'),
    ('General', 'Casework', 'OST_Casework'),
]


def category_options():
    """Only offer categories that exist in this Revit version AND are present
    in this document. getattr alone is not enough - a BuiltInCategory can
    exist in the API while the category is absent from the model."""
    options = []
    for discipline, label, enum_name in CATEGORY_CANDIDATES:
        enum_value = getattr(BuiltInCategory, enum_name, None)
        if enum_value is None:
            continue
        try:
            if Category.GetCategory(doc, enum_value) is None:
                continue
        except Exception:
            continue
        options.append(('{0}  |  {1}'.format(discipline, label), enum_value))
    return options


CATEGORY_OPTIONS = category_options()


def category_elements(labels):
    """ElementMulticategoryFilter raises ArgumentException if a single member
    of the set is not filterable, which would take down the whole selection.
    Fall back to per-category filters so one bad category cannot block the
    other twenty."""
    wanted = [category for label, category in CATEGORY_OPTIONS if label in labels]
    if not wanted:
        return []
    try:
        element_filter = ElementMulticategoryFilter(List[BuiltInCategory](wanted))
        return list(FilteredElementCollector(doc)
                    .WherePasses(element_filter)
                    .WhereElementIsNotElementType()
                    .ToElements())
    except Exception:
        pass
    filters = []
    for category in wanted:
        try:
            filters.append(ElementCategoryFilter(category))
        except Exception:
            pass
    if not filters:
        return []
    combined = filters[0]
    for extra in filters[1:]:
        combined = LogicalOrFilter(combined, extra)
    try:
        return list(FilteredElementCollector(doc)
                    .WherePasses(combined)
                    .WhereElementIsNotElementType()
                    .ToElements())
    except Exception:
        return []


# ==========================================================================
# Schedules
# ==========================================================================
def schedule_options():
    results = []
    for schedule in FilteredElementCollector(doc).OfClass(ViewSchedule).WhereElementIsNotElementType():
        try:
            if schedule.IsTemplate:
                continue
            if not schedule.Name or schedule.Name.startswith('<'):
                continue
            results.append(schedule)
        except Exception:
            pass
    return sorted(results, key=lambda item: item.Name.lower())


def schedule_elements(schedule):
    if schedule is None:
        return []
    try:
        return list(FilteredElementCollector(doc, schedule.Id)
                    .WhereElementIsNotElementType()
                    .ToElements())
    except Exception:
        return []


def schedule_visible_fields(schedule):
    fields = []
    try:
        definition = schedule.Definition
    except Exception:
        return fields
    for field_id in definition.GetFieldOrder():
        try:
            field = definition.GetField(field_id)
        except Exception:
            continue
        if field is not None and not field.IsHidden:
            fields.append(field)
    return fields


def field_parameter_name(field):
    """The underlying parameter name, not the (possibly renamed) column
    heading. Only the real name resolves on import."""
    try:
        schedulable = field.GetSchedulableField()
        if schedulable is not None:
            name = schedulable.GetName(doc)
            if name:
                return name
    except Exception:
        pass
    try:
        return field.GetName()
    except Exception:
        return ''


# ==========================================================================
# Parameter access
# ==========================================================================
def parameter_index(element, allow_type):
    """One pass over an element's parameters, reused for every column in the
    row. Keyed by name and by parameter id so schedule fields that share a
    display name can still be disambiguated."""
    index = {}
    try:
        for parameter in element.Parameters:
            try:
                name = parameter.Definition.Name
            except Exception:
                continue
            if name not in index:
                index[name] = parameter
            key = '#' + str(id_value(parameter.Id))
            if key not in index:
                index[key] = parameter
    except Exception:
        pass
    if allow_type:
        symbol = element_type(element)
        if symbol is not None:
            try:
                for parameter in symbol.Parameters:
                    try:
                        name = parameter.Definition.Name
                    except Exception:
                        continue
                    type_key = 't:' + name
                    if type_key not in index:
                        index[type_key] = parameter
                    key = 't#' + str(id_value(parameter.Id))
                    if key not in index:
                        index[key] = parameter
            except Exception:
                pass
    return index


def resolve(index, name, allow_type, field_ids=None):
    """Instance parameter wins. Type parameter is the fallback, and only when
    the user has opted in - writing a type parameter changes every instance
    of that type, which is rarely what a per-row spreadsheet import means.

    field_ids: {parameter name: ElementId} of one schedule's fields. When
    several schedules are exported together each one passes its own map, so
    a name used by two schedules cannot pick up the other one's parameter."""
    param_id = (schedule_field_ids if field_ids is None else field_ids).get(name)
    if param_id is not None:
        parameter = index.get('#' + str(id_value(param_id)))
        if parameter is not None:
            return parameter, 'instance'
        if allow_type:
            parameter = index.get('t#' + str(id_value(param_id)))
            if parameter is not None:
                return parameter, 'type'
    parameter = index.get(name)
    if parameter is not None:
        return parameter, 'instance'
    if allow_type:
        parameter = index.get('t:' + name)
        if parameter is not None:
            return parameter, 'type'
    return None, None


def parameter_names(elements, include_type):
    """Sampled by element type rather than by element. A 40,000 pipe model has
    maybe 30 pipe types, so this is orders of magnitude faster than walking
    every instance and produces the same name set in practice."""
    names = set()
    seen = set()
    for element in elements:
        try:
            key = id_value(element.GetTypeId())
        except Exception:
            key = -1
        if key in seen:
            continue
        seen.add(key)
        try:
            for parameter in element.Parameters:
                try:
                    names.add(parameter.Definition.Name)
                except Exception:
                    pass
        except Exception:
            pass
        if include_type:
            symbol = element_type(element)
            if symbol is not None:
                try:
                    for parameter in symbol.Parameters:
                        try:
                            names.add(parameter.Definition.Name)
                        except Exception:
                            pass
                except Exception:
                    pass
        if len(seen) >= TYPE_SAMPLE_LIMIT:
            break
    return sorted(names, key=lambda value: value.lower())


def is_yes_no(parameter):
    if SpecTypeId is not None:
        try:
            if parameter.Definition.GetDataType() == SpecTypeId.Boolean.YesNo:
                return True
        except Exception:
            pass
    if ParameterType is not None:
        try:
            return parameter.Definition.ParameterType == ParameterType.YesNo
        except Exception:
            pass
    return False


def to_internal(parameter, number):
    """Revit stores doubles in internal units (feet, cfs, ...). Writing a
    display-unit number straight in silently scales the value - a 300 mm duct
    becomes 300 feet. Convert through the parameter's own unit."""
    try:
        unit = parameter.GetUnitTypeId()
        if unit is not None and not unit.Empty():
            return UnitUtils.ConvertToInternalUnits(number, unit)
    except Exception:
        pass
    try:
        return UnitUtils.ConvertToInternalUnits(number, parameter.DisplayUnitType)
    except Exception:
        pass
    return number


def from_internal(parameter, number):
    try:
        unit = parameter.GetUnitTypeId()
        if unit is not None and not unit.Empty():
            return UnitUtils.ConvertFromInternalUnits(number, unit)
    except Exception:
        pass
    try:
        return UnitUtils.ConvertFromInternalUnits(number, parameter.DisplayUnitType)
    except Exception:
        pass
    return number


def name_lookup():
    """name -> ElementId for things an ElementId parameter can point at:
    levels, materials, MEP system types, phases, and element types."""
    global name_lookup_cache
    if name_lookup_cache is not None:
        return name_lookup_cache
    lookup = {}

    def add(element):
        try:
            name = Element.Name.GetValue(element)
        except Exception:
            return
        if name:
            lookup.setdefault(name.strip().lower(), element.Id)

    for cls in (Level, Material, MEPSystemType, Phase, ElementType):
        if cls is None:
            continue
        try:
            for element in FilteredElementCollector(doc).OfClass(cls):
                add(element)
        except Exception:
            pass
    name_lookup_cache = lookup
    return lookup


TRUE_WORDS = set(['yes', 'y', 'true', 't', '1', 'on', 'checked', 'x'])
FALSE_WORDS = set(['no', 'n', 'false', 'f', '0', 'off', 'unchecked', ''])

NUMBER_PATTERN = re.compile(r'-?(?:[0-9][0-9,]*(?:\.[0-9]+)?|\.[0-9]+)(?:[eE][-+]?[0-9]+)?')


def to_number(text):
    match = NUMBER_PATTERN.search(str(text))
    if not match:
        raise ValueError('no numeric value found in "{0}"'.format(text))
    return float(match.group().replace(',', ''))


def clean(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number != number or number in (float('inf'), float('-inf')):
        return str(value)                      # NaN / Infinity: int() would raise
    if number == int(number) and abs(number) < 1e15:
        return str(int(number))
    text = repr(number)
    if 'e' in text or 'E' in text:
        # 1e-05 / 1e+20: never strip zeros off an exponent ('1e+20' -> '1e+2')
        text = '{0:.12f}'.format(number) if abs(number) < 1 else '{0:.0f}'.format(number)
    if '.' in text:
        text = text.rstrip('0').rstrip('.')
    return text


def normalize_key(value):
    """Both sides of a match go through this. Previously Excel keys were
    normalized and Revit values were not, so '100' never matched '100.00'."""
    if value is None:
        return ''
    text = str(value).strip()
    if not text:
        return ''
    try:
        return clean(float(text.replace(',', '')))
    except (TypeError, ValueError):
        return ' '.join(text.split())


# ==========================================================================
# Reading a value out of Revit
# ==========================================================================
def read_value(parameter, with_units):
    if parameter is None:
        return None
    try:
        storage = parameter.StorageType
        if storage == StorageType.String:
            result = parameter.AsString()
            return result.strip() if result and result.strip() else None
        if storage == StorageType.Integer:
            if is_yes_no(parameter):
                return 'Yes' if parameter.AsInteger() else 'No'
            return str(parameter.AsInteger())
        if storage == StorageType.ElementId:
            target = parameter.AsElementId()
            if target is None or target == ElementId.InvalidElementId:
                return None
            if id_value(target) < 0:
                return parameter.AsValueString()
            element = doc.GetElement(target)
            if element is None:
                return None
            try:
                return Element.Name.GetValue(element)
            except Exception:
                return parameter.AsValueString()
        if storage == StorageType.Double:
            formatted = parameter.AsValueString()
            if with_units:
                if formatted and formatted.strip():
                    return formatted.strip()
                return clean(from_internal(parameter, parameter.AsDouble()))
            # Without units: "0 A" must not reach Excel, which reads it as
            # "0 AM" and converts it to a time. Convert the stored double
            # directly instead of parsing the formatted string: parsing broke
            # on comma-decimal projects ("1,5 m" -> 15) and on space
            # thousands separators ("1 200 mm" -> 1), and lost precision to
            # display rounding. 12 significant digits drop float noise
            # (299.99999999999994 -> 300).
            value = from_internal(parameter, parameter.AsDouble())
            return clean(float('{0:.12g}'.format(value)))
    except Exception:
        return None
    return None


# ==========================================================================
# Writing a value into Revit
# ==========================================================================
def write_value(parameter, item):
    """Returns (success, reason)."""
    if parameter is None:
        return False, 'parameter not found on element'
    if parameter.IsReadOnly:
        return False, 'read-only parameter (calculated or derived)'
    if item is None or str(item).strip() == '':
        return False, 'blank value'
    text = str(item).strip()
    try:
        storage = parameter.StorageType

        if storage == StorageType.String:
            parameter.Set(text)
            return True, ''

        if storage == StorageType.Integer:
            if is_yes_no(parameter):
                lowered = text.lower()
                if lowered in TRUE_WORDS:
                    parameter.Set(1)
                    return True, ''
                if lowered in FALSE_WORDS:
                    parameter.Set(0)
                    return True, ''
                return False, 'not a yes/no value'
            try:
                if parameter.SetValueString(text):
                    return True, ''
            except Exception:
                pass
            parameter.Set(int(round(to_number(text))))
            return True, ''

        if storage == StorageType.Double:
            # SetValueString parses exactly what Revit displays, units and
            # thousands separators included, in the project's display units.
            try:
                if parameter.SetValueString(text):
                    return True, ''
            except Exception:
                pass
            number = to_number(text)
            parameter.Set(to_internal(parameter, number))
            return True, ''

        if storage == StorageType.ElementId:
            stripped = text.replace(',', '')
            if re.match(r'^-?\d+$', stripped):
                candidate = make_element_id(int(stripped))
                if doc.GetElement(candidate) is not None:
                    parameter.Set(candidate)
                    return True, ''
            target = name_lookup().get(text.lower())
            if target is not None:
                parameter.Set(target)
                return True, ''
            return False, 'no element named "{0}" found for this parameter'.format(text)

        return False, 'unsupported parameter storage type'
    except ValueError as error:
        return False, str(error)
    except Exception as error:
        return False, str(error)


# ==========================================================================
# XLSX reading
# ==========================================================================
def xlsx_xml(path, entry_name):
    # ZipFile.OpenRead asks for FileShare.Read, which fails with "being used
    # by another process" while the workbook is open in Excel. Open the
    # stream with FileShare.ReadWrite so an open workbook can still be read
    # (Excel must have saved the edits for them to be seen).
    stream_in = FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite)
    try:
        archive = ZipArchive(stream_in, ZipArchiveMode.Read)
    except Exception:
        stream_in.Dispose()
        raise
    try:
        entry = archive.GetEntry(entry_name)
        if entry is None:
            return None
        stream = entry.Open()
        reader = StreamReader(stream)
        try:
            content = reader.ReadToEnd()
        finally:
            reader.Close()
        document = XmlDocument()
        document.LoadXml(content)
        return document
    finally:
        archive.Dispose()
        stream_in.Dispose()


BUILTIN_DATE_FORMATS = set(range(14, 23)) | set([45, 46, 47])
QUOTED = re.compile(r'"[^"]*"|\[[^\]]*\]|\\.')


def looks_like_date_format(code):
    if not code:
        return False
    stripped = QUOTED.sub('', code).lower()
    if 'general' in stripped:
        return False
    return any(character in stripped for character in 'ymdh')


def load_styles(path):
    """Excel stores dates as serial numbers; only the cell style says it is a
    date. Without this, a date column imports as '45678'."""
    styles = set()
    document = xlsx_xml(path, 'xl/styles.xml')
    if document is None:
        return styles
    custom = {}
    for node in document.GetElementsByTagName('numFmt'):
        try:
            custom[node.GetAttribute('numFmtId')] = node.GetAttribute('formatCode')
        except Exception:
            pass
    containers = document.GetElementsByTagName('cellXfs')
    if containers.Count == 0:
        return styles
    position = 0
    for xf in containers[0].ChildNodes:
        try:
            fmt_id = xf.GetAttribute('numFmtId')
        except Exception:
            position += 1
            continue
        if fmt_id:
            try:
                numeric_id = int(fmt_id)
            except ValueError:
                numeric_id = -1
            if numeric_id in BUILTIN_DATE_FORMATS or looks_like_date_format(custom.get(fmt_id)):
                styles.add(position)
        position += 1
    return styles


def serial_to_text(serial):
    try:
        days = int(serial)
        fraction = float(serial) - days
        moment = DateTime(1899, 12, 30).AddDays(days).AddSeconds(int(round(fraction * 86400)))
        if fraction:
            return moment.ToString('yyyy-MM-dd HH:mm:ss')
        return moment.ToString('yyyy-MM-dd')
    except Exception:
        return str(serial)


def cell_text(cell):
    """Read only the value node. cell.InnerText concatenates every child, so a
    formula cell <c><f>SUM(A1:A9)</f><v>42</v></c> came back as 'SUM(A1:A9)42'."""
    cell_type = cell.GetAttribute('t')
    if cell_type == 'inlineStr':
        for child in cell.ChildNodes:
            if child.LocalName == 'is':
                return child.InnerText
        return ''
    raw = None
    for child in cell.ChildNodes:
        if child.LocalName == 'v':
            raw = child.InnerText
            break
    if raw is None:
        return ''
    if cell_type == 's':
        try:
            return shared_strings[int(raw)]
        except (ValueError, IndexError):
            return ''
    if cell_type == 'b':
        return 'Yes' if raw == '1' else 'No'
    if cell_type == 'e':
        return ''
    if cell_type in ('str', 'd'):
        return raw
    style = cell.GetAttribute('s')
    if style:
        try:
            if int(style) in date_styles:
                return serial_to_text(float(raw))
        except (ValueError, TypeError):
            pass
    # Excel writes small / large numbers in scientific notation in the XML
    # ("1.2E-3"). Passed on raw, the number parser read that as 1.2.
    try:
        return clean(float(raw))
    except (ValueError, TypeError):
        return raw


def column_index(reference):
    letters = ''.join(character for character in reference if character.isalpha())
    index = 0
    for letter in letters:
        index = index * 26 + ord(letter.upper()) - 64
    return index - 1


def open_excel(path):
    global xlsx_path, xlsx_sheets, shared_strings, date_styles
    extension = Path.GetExtension(path).lower()
    if extension not in ('.xlsx', '.xlsm'):
        forms.alert('Only .xlsx and .xlsm are supported. Save legacy .xls files as .xlsx first.', title=TITLE)
        return False
    try:
        xlsx_path = path
        workbook_document = xlsx_xml(path, 'xl/workbook.xml')
        relationships_document = xlsx_xml(path, 'xl/_rels/workbook.xml.rels')
        if workbook_document is None or relationships_document is None:
            raise Exception('This file is not a valid Open XML workbook.')
        relationship_namespace = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
        relationships = {}
        for relation in relationships_document.GetElementsByTagName('Relationship'):
            relationships[relation.GetAttribute('Id')] = relation.GetAttribute('Target')
        sheets = []
        for sheet in workbook_document.GetElementsByTagName('sheet'):
            if sheet.GetAttribute('state') in ('hidden', 'veryHidden'):
                continue
            relation_id = sheet.GetAttribute('id', relationship_namespace)
            if not relation_id:
                # Strict OOXML uses a different namespace; take any r:id.
                for attribute in sheet.Attributes:
                    if attribute.LocalName == 'id' and attribute.Prefix:
                        relation_id = attribute.Value
                        break
            target = relationships.get(relation_id)
            if not target:
                continue
            target = target.replace('\\', '/')
            # Targets are relative to xl/ ("worksheets/sheet1.xml") or
            # package-absolute ("/xl/worksheets/sheet1.xml", as written by
            # openpyxl and others). The absolute form used to become
            # "xl/xl/worksheets/..." and the sheet could not be opened.
            if target.startswith('/'):
                entry_name = target.lstrip('/')
            elif target.startswith('xl/'):
                entry_name = target
            else:
                entry_name = 'xl/' + target
            sheets.append((sheet.GetAttribute('name'), entry_name))
        if not sheets:
            raise Exception('No visible worksheets were found.')
        xlsx_sheets = sheets                  # workbook order preserved
        shared_strings = []
        strings_document = xlsx_xml(path, 'xl/sharedStrings.xml')
        if strings_document is not None:
            for string_item in strings_document.GetElementsByTagName('si'):
                parts = []
                for child in string_item.ChildNodes:
                    if child.LocalName == 't':
                        parts.append(child.InnerText)
                    elif child.LocalName == 'r':
                        for run_child in child.ChildNodes:
                            if run_child.LocalName == 't':
                                parts.append(run_child.InnerText)
                shared_strings.append(''.join(parts) if parts else string_item.InnerText)
        date_styles = load_styles(path)
        return True
    except Exception as error:
        xlsx_path = None
        xlsx_sheets = []
        shared_strings = []
        date_styles = set()
        forms.alert('The workbook could not be read.\n\n{0}'.format(error), title=TITLE)
        return False


def read_sheet(entry_name):
    global columns, sheet_data
    document = xlsx_xml(xlsx_path, entry_name)
    if document is None:
        raise Exception('The worksheet could not be opened.')
    rows = []
    for row_node in document.GetElementsByTagName('row'):
        row = {}
        maximum = -1
        for cell in row_node.GetElementsByTagName('c'):
            # The r="B4" reference is optional in the spec; when absent the
            # cell is the one after the previous cell. Such cells used to be
            # dropped, so some generated workbooks loaded as empty.
            reference = cell.GetAttribute('r')
            index = column_index(reference) if reference else maximum + 1
            if index < 0:
                continue
            row[index] = cell_text(cell)
            maximum = max(maximum, index)
        if maximum >= 0:
            rows.append([row.get(index) for index in range(maximum + 1)])
    if not rows:
        raise Exception('The worksheet is empty.')

    header_row = rows[0]
    headers = [(clean(cell).strip() if cell is not None else '') for cell in header_row]
    if not any(headers):
        raise Exception('The first row must contain column headers.')

    # Duplicate headers are common in multi-category schedule exports. Give
    # each a unique display label but keep the real parameter name for lookup.
    columns = []
    seen = {}
    for index, header in enumerate(headers):
        if not header:
            continue
        count = seen.get(header, 0) + 1
        seen[header] = count
        display = header if count == 1 else '{0} ({1})'.format(header, count)
        columns.append((display, header, index))
    sheet_data = rows[1:]


def column_by_display(display):
    for entry in columns:
        if entry[0] == display:
            return entry
    return None


def excel_rows():
    selected = window.excel_key.SelectedItem
    if not columns or not selected:
        return {}
    entry = column_by_display(str(selected))
    if entry is None:
        return {}
    index = entry[2]
    result = {}
    duplicates = 0
    for row in sheet_data:
        if index < len(row) and row[index] is not None:
            key = normalize_key(row[index])
            if key:
                if key in result:
                    duplicates += 1
                result[key] = row
    if duplicates:
        window.duplicate_note.Text = '{0} duplicate key(s) in the sheet; the last row wins.'.format(duplicates)
        window.duplicate_note.Visibility = Visibility.Visible
    else:
        window.duplicate_note.Visibility = Visibility.Collapsed
    return result


# ==========================================================================
# XLSX writing - minimal Open XML package, no Excel dependency
# ==========================================================================
ILLEGAL_XML = re.compile(u'[\x00-\x08\x0b\x0c\x0e-\x1f]')


def escape(text):
    text = ILLEGAL_XML.sub('', str(text))
    return (text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                .replace('"', '&quot;'))


def column_letter(index):
    letters = ''
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


NUMERIC_CELL = re.compile(r'^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$')


def cell_xml(reference, value):
    if value is None or value == '':
        return ''
    text = str(value)
    # Only write a numeric cell when Excel will give back exactly the same
    # text. "1.50", "10.10" or a 17-digit code would otherwise come back as
    # "1.5", "10.1" or 1.2E+16 and overwrite text parameters on import.
    if NUMERIC_CELL.match(text) and len(text.lstrip('-').replace('.', '')) <= 15 \
            and clean(float(text)) == text:
        return '<c r="{0}"><v>{1}</v></c>'.format(reference, text)
    return ('<c r="{0}" t="inlineStr"><is><t xml:space="preserve">{1}</t></is></c>'
            .format(reference, escape(text)))


def sheet_xml(rows):
    parts = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
             '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
             '<sheetData>']
    for row_number, row in enumerate(rows, start=1):
        parts.append('<row r="{0}">'.format(row_number))
        for column_number, value in enumerate(row):
            parts.append(cell_xml(column_letter(column_number) + str(row_number), value))
        parts.append('</row>')
    parts.append('</sheetData></worksheet>')
    return ''.join(parts)


INVALID_SHEET_CHARS = re.compile(r'[\[\]\*\?/\\:]')


def safe_sheet_name(name, used):
    cleaned = INVALID_SHEET_CHARS.sub('-', str(name)).strip() or 'Sheet'
    cleaned = cleaned[:31]
    candidate = cleaned
    suffix = 2
    while candidate.lower() in used:
        tail = ' ({0})'.format(suffix)
        candidate = cleaned[:31 - len(tail)] + tail
        suffix += 1
    used.add(candidate.lower())
    return candidate


def write_xlsx(path, sheets):
    """sheets: [(name, [[cell, ...], ...])]"""
    if File.Exists(path):
        File.Delete(path)
    encoding = UTF8Encoding(False)
    used = set()
    named = [(safe_sheet_name(name, used), rows) for name, rows in sheets]

    content_types = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
                     '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
                     '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
                     '<Default Extension="xml" ContentType="application/xml"/>',
                     '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>']
    workbook_sheets = []
    workbook_rels = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
                     '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">']
    for position, (name, _) in enumerate(named, start=1):
        content_types.append('<Override PartName="/xl/worksheets/sheet{0}.xml" '
                             'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                             .format(position))
        workbook_sheets.append('<sheet name="{0}" sheetId="{1}" r:id="rId{1}"/>'.format(escape(name), position))
        workbook_rels.append('<Relationship Id="rId{0}" '
                             'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
                             'Target="worksheets/sheet{0}.xml"/>'.format(position))
    content_types.append('</Types>')
    workbook_rels.append('</Relationships>')

    parts = [
        ('[Content_Types].xml', ''.join(content_types)),
        ('_rels/.rels', '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                        '<Relationship Id="rId1" '
                        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
                        'Target="xl/workbook.xml"/></Relationships>'),
        ('xl/workbook.xml', '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                            '<sheets>' + ''.join(workbook_sheets) + '</sheets></workbook>'),
        ('xl/_rels/workbook.xml.rels', ''.join(workbook_rels)),
    ]
    for position, (_, rows) in enumerate(named, start=1):
        parts.append(('xl/worksheets/sheet{0}.xml'.format(position), sheet_xml(rows)))

    archive = ZipFile.Open(path, ZipArchiveMode.Create)
    try:
        for entry_name, xml in parts:
            entry = archive.CreateEntry(entry_name, CompressionLevel.Optimal)
            stream = entry.Open()
            writer = StreamWriter(stream, encoding)
            try:
                writer.Write(xml)
                writer.Flush()
            finally:
                writer.Close()
    finally:
        archive.Dispose()


INVALID_FILE_CHARS = re.compile(u'[<>:"/\\\\|?*\x00-\x1f]')


def safe_file_name(name, used=None):
    """A schedule name as a Windows file name. Pass a set as `used` to also
    keep the names of one batch unique."""
    cleaned = INVALID_FILE_CHARS.sub('-', str(name)).strip(' .') or 'Schedule'
    cleaned = cleaned[:120]
    if used is None:
        return cleaned
    candidate = cleaned
    suffix = 2
    while candidate.lower() in used:
        candidate = '{0} ({1})'.format(cleaned, suffix)
        suffix += 1
    used.add(candidate.lower())
    return candidate


def save_path(default_name, workbook=True, allow_csv=True):
    dialog = SaveFileDialog()
    default_name = safe_file_name(default_name)
    if workbook:
        dialog.Filter = 'Excel Workbook (*.xlsx)|*.xlsx'
        if allow_csv:          # a CSV file cannot hold more than one sheet
            dialog.Filter += '|Excel-compatible CSV (*.csv)|*.csv'
        dialog.FileName = default_name + '.xlsx'
    else:
        dialog.Filter = 'Excel-compatible CSV (*.csv)|*.csv'
        dialog.FileName = default_name + '.csv'
    if dialog.ShowDialog() != DialogResult.OK:
        return None
    return dialog.FileName


def pick_folder(description):
    dialog = FolderBrowserDialog()
    dialog.Description = description
    if dialog.ShowDialog() != DialogResult.OK:
        return None
    return dialog.SelectedPath


CSV_INJECTION = ('=', '+', '-', '@', '\t', '\r')


def csv_value(value):
    text = '' if value is None else str(value)
    # A plain negative number ("-300") is data, not a formula: prefixing it
    # turned every negative offset into the text "'-300".
    if text[:1] in CSV_INJECTION and not NUMERIC_CELL.match(text):
        text = "'" + text            # neutralize spreadsheet formula injection
    return '"{0}"'.format(text.replace('"', '""'))


def write_csv(path, rows):
    writer = StreamWriter(path, False, Encoding.UTF8)
    try:
        for row in rows:
            writer.WriteLine(','.join(csv_value(value) for value in row))
    finally:
        writer.Close()


def write_table(path, sheet_name, rows):
    if path.lower().endswith('.csv'):
        write_csv(path, rows)
    else:
        write_xlsx(path, [(sheet_name, rows)])


# ==========================================================================
# Working element set
# ==========================================================================
def selected_elements():
    """The elements every action works on: the working set, narrowed by the
    ticked families and by the applied parameter-value rules."""
    wanted = checked_labels(window.families)
    if not wanted:
        # Families are listed but none ticked ("None" button): that means
        # NOTHING, not everything. It used to import into every element.
        if window.families.Items.Count:
            return []
        elements = list(all_elements)
    else:
        elements = [item for item in all_elements if family(item) in wanted]
    if active_rules:
        allow_type = bool(window.allow_type_params.IsChecked)
        elements = [item for item in elements if passes_rules(item, active_rules, allow_type)]
    return elements


# ==========================================================================
# Filter by parameter value
# ==========================================================================
FILTER_OPS = ['contains', 'does not contain', 'equals', 'does not equal',
              'starts with', 'ends with', 'is empty', 'is not empty',
              'greater than', 'less than']
NO_VALUE_OPS = ('is empty', 'is not empty')
# pseudo "parameters" offered above the real ones
FIELD_CATEGORY, FIELD_FAMILY, FIELD_TYPE = '<Category>', '<Family>', '<Type>'
NO_RULE = '(no rule)'
active_rules = []            # [(parameter name, operator, value)] - the APPLIED rules


def filter_text(element, name, allow_type):
    """The text a rule is tested against: the parameter's value as Revit shows
    it (without the unit suffix), instance first, then type if allowed."""
    if name == FIELD_CATEGORY:
        return category_name(element)
    if name == FIELD_FAMILY:
        return family(element)
    if name == FIELD_TYPE:
        return type_name(element)
    parameter = None
    try:
        parameter = element.LookupParameter(name)
    except Exception:
        parameter = None
    if parameter is None and allow_type:
        symbol = element_type(element)
        if symbol is not None:
            try:
                parameter = symbol.LookupParameter(name)
            except Exception:
                parameter = None
    value = read_value(parameter, False)
    return '' if value is None else str(value)


def rule_passes(text, operator, value):
    """Case-insensitive. 'equals' also treats 100 and 100.00 as the same."""
    actual = ' '.join((text or '').split()).lower()
    wanted = ' '.join((value or '').split()).lower()
    if operator == 'is empty':
        return actual == ''
    if operator == 'is not empty':
        return actual != ''
    if operator == 'contains':
        return wanted in actual
    if operator == 'does not contain':
        return wanted not in actual
    if operator in ('equals', 'does not equal'):
        same = actual == wanted or (actual != '' and normalize_key(actual) == normalize_key(wanted))
        return same if operator == 'equals' else not same
    if operator == 'starts with':
        return actual.startswith(wanted)
    if operator == 'ends with':
        return actual.endswith(wanted)
    if operator in ('greater than', 'less than'):
        try:
            left, right = to_number(actual), to_number(wanted)
        except ValueError:
            return False               # not a number: cannot be greater / less
        return left > right if operator == 'greater than' else left < right
    return True


def passes_rules(element, rules, allow_type):
    for name, operator, value in rules:
        if not rule_passes(filter_text(element, name, allow_type), operator, value):
            return False
    return True


def rule_controls():
    return [(window.filter1_param, window.filter1_op, window.filter1_value),
            (window.filter2_param, window.filter2_op, window.filter2_value)]


def rules_in_ui():
    """Complete rules as currently typed (whether applied yet or not)."""
    rules = []
    for param_box, op_box, value_box in rule_controls():
        name = str(param_box.SelectedItem) if param_box.SelectedItem else NO_RULE
        operator = str(op_box.SelectedItem) if op_box.SelectedItem else FILTER_OPS[0]
        value = (value_box.Text or '').strip()
        if name == NO_RULE:
            continue
        if operator not in NO_VALUE_OPS and not value:
            continue
        rules.append((name, operator, value))
    return rules


def set_rules_in_ui(rules):
    rules = list(rules or [])
    for position, (param_box, op_box, value_box) in enumerate(rule_controls()):
        name, operator, value = rules[position] if position < len(rules) else (NO_RULE, FILTER_OPS[0], '')
        available = [str(item) for item in param_box.Items]
        if name not in available:
            param_box.Items.Add(name)     # keep a rule on a parameter this model lacks visible
        param_box.SelectedItem = name
        op_box.SelectedItem = operator if operator in FILTER_OPS else FILTER_OPS[0]
        value_box.Text = value


def fill_rule_parameters(names):
    """Refill the two parameter pickers, keeping what was selected."""
    for param_box, _, _ in rule_controls():
        previous = str(param_box.SelectedItem) if param_box.SelectedItem else NO_RULE
        param_box.Items.Clear()
        for name in [NO_RULE, FIELD_CATEGORY, FIELD_FAMILY, FIELD_TYPE] + list(names):
            param_box.Items.Add(name)
        if previous not in [str(item) for item in param_box.Items]:
            param_box.Items.Add(previous)
        param_box.SelectedItem = previous


def update_filter_note():
    if not active_rules:
        pending = rules_in_ui()
        window.filter_note.Text = 'Not applied yet' if pending else 'No filter'
        return
    total = len(all_elements)
    window.filter_note.Text = '{0} of {1} pass'.format(len(selected_elements()), total)


def apply_filter(sender=None, args=None):
    global active_rules
    active_rules = rules_in_ui()
    busy(True)
    try:
        update_filter_note()
        if xlsx_sheets and columns:
            preview()                  # the preview shows the filtered set straight away
        else:
            window.match_list.Items.Clear()
            window.summary.Text = '{0} element(s) pass the filter.'.format(len(selected_elements())) \
                if active_rules else '{0} element(s), no filter.'.format(len(all_elements))
    finally:
        busy(False)


def clear_filter(sender=None, args=None):
    set_rules_in_ui([])
    apply_filter()


def filter_enter(sender, args):
    from System.Windows.Input import Key
    if args.Key == Key.Return or args.Key == Key.Enter:
        apply_filter()


def apply_working_set(elements, description):
    global all_elements
    all_elements = elements

    previous_families = checked_labels(window.families) if window.families.Items.Count else None
    previous_listed = all_labels(window.families)
    everything_was_ticked = previous_families is not None and previous_families == previous_listed
    window.families.Items.Clear()
    for name in sorted(set(family(item) for item in all_elements), key=lambda item: item.lower()):
        # First population defaults to checked. Previously it defaulted to
        # unchecked, so a fresh run always previewed and imported zero rows.
        if previous_families is None:
            is_checked = True
        elif name in previous_listed:
            is_checked = name in previous_families
        else:
            # A family that was not listed before (another category or
            # schedule was just ticked). It came in UNTICKED, so widening
            # the source silently left the new elements out. It now follows
            # the list: ticked when everything else was ticked.
            is_checked = everything_was_ticked
        window.families.Items.Add(checkbox_item(name, is_checked))
    filter_listbox(window.families, window.families_search.Text)

    include_type = bool(window.allow_type_params.IsChecked)
    names = parameter_names(all_elements, include_type)

    previous_key = window.revit_key.SelectedItem
    window.revit_key.Items.Clear()
    for name in names:
        window.revit_key.Items.Add(name)
    available = [str(item) for item in window.revit_key.Items]
    if previous_key and str(previous_key) in available:
        window.revit_key.SelectedItem = previous_key
    elif window.revit_key.Items.Count:
        window.revit_key.SelectedIndex = 0

    previous_export = checked_labels(window.export_params) if window.export_params.Items.Count else set()
    window.export_params.Items.Clear()
    for name in names:
        window.export_params.Items.Add(checkbox_item(name, name in previous_export))
    filter_listbox(window.export_params, window.export_params_search.Text)

    fill_rule_parameters(names)
    update_filter_note()

    window.match_list.Items.Clear()
    window.summary.Text = '{0} element(s) found {1}.'.format(len(all_elements), description)
    if active_rules:
        window.summary.Text += ' {0} pass the parameter filter.'.format(len(selected_elements()))


refresh_hold = [False]       # True while All / None ticks many boxes at once


def tick_many(listbox, state, refresh):
    """All / None for a list whose tick boxes each trigger a model query:
    tick everything first, then query ONCE instead of once per box."""
    refresh_hold[0] = True
    try:
        if state:
            check_all(listbox)
        else:
            check_none(listbox)
    finally:
        refresh_hold[0] = False
    refresh()


def refresh_categories(sender=None, args=None):
    if refresh_hold[0] or not window.category_mode.IsChecked:
        return
    global schedule_field_ids
    busy(True)
    try:
        schedule_field_ids = {}
        selected = checked_labels(window.categories)
        elements = category_elements(selected)
        apply_working_set(elements, 'across {0} selected categor{1}'.format(
            len(selected), 'y' if len(selected) == 1 else 'ies'))
    finally:
        busy(False)


def ticked_schedules():
    """Ticked schedules, in the order of the list."""
    wanted = checked_labels(window.schedules)
    return [schedule for schedule in ALL_SCHEDULES if schedule.Name in wanted]


def refresh_schedule(sender=None, args=None):
    """The working set (families, parameters, import targets) is the union of
    the elements of every ticked schedule."""
    if refresh_hold[0] or not window.schedule_mode.IsChecked:
        return
    global schedule_field_ids
    busy(True)
    try:
        chosen = ticked_schedules()
        window.schedules_count.Text = '{0} of {1} ticked'.format(len(chosen), len(ALL_SCHEDULES))
        schedule_field_ids = {}
        elements = []
        seen = set()
        for schedule in chosen:
            for element in schedule_elements(schedule):
                key = id_value(element.Id)
                if key not in seen:
                    seen.add(key)
                    elements.append(element)
            for field in schedule_visible_fields(schedule):
                try:
                    param_id = field.ParameterId
                    if param_id is not None and param_id != ElementId.InvalidElementId:
                        schedule_field_ids.setdefault(field_parameter_name(field), param_id)
                except Exception:
                    pass
        if not chosen:
            description = 'in schedules (none ticked)'
        elif len(chosen) == 1:
            description = 'in schedule "{0}"'.format(chosen[0].Name)
        else:
            description = 'in {0} ticked schedules'.format(len(chosen))
        apply_working_set(elements, description)
    finally:
        busy(False)


def refresh_source(sender=None, args=None):
    if window.schedule_mode.IsChecked:
        refresh_schedule()
    else:
        refresh_categories()


def set_mode_category(sender=None, args=None):
    window.category_panel.Visibility = Visibility.Visible
    window.schedule_panel.Visibility = Visibility.Collapsed
    refresh_categories()


def set_mode_schedule(sender=None, args=None):
    window.category_panel.Visibility = Visibility.Collapsed
    window.schedule_panel.Visibility = Visibility.Visible
    refresh_schedule()


# ==========================================================================
# Excel side of the UI
# ==========================================================================
def refresh_import_columns(sender=None, args=None):
    selected_key = str(window.excel_key.SelectedItem) if window.excel_key.SelectedItem else None
    window.excel_columns.Items.Clear()
    for display, header, _ in columns:
        if display == selected_key:
            continue
        if header.lower() in ('element id', 'category', 'family', 'type', 'family and type',
                              'source schedule'):
            continue          # identity columns, never writable
        window.excel_columns.Items.Add(checkbox_item(display, True))
    filter_listbox(window.excel_columns, window.excel_columns_search.Text)


def open_workbook(path, sheet_name=None):
    """Open a workbook and list its sheets; selects `sheet_name` when it
    exists, else the first sheet. Returns True when the workbook opened."""
    window.file_path.Text = path
    if not open_excel(path):
        return False
    window.sheets.Items.Clear()
    names = [name for name, _ in xlsx_sheets]
    for name in names:
        window.sheets.Items.Add(name)
    if names:
        window.sheets.SelectedIndex = names.index(sheet_name) if sheet_name in names else 0
    return True


def browse(sender, args):
    dialog = OpenFileDialog()
    dialog.Filter = 'Excel Workbooks (*.xlsx;*.xlsm)|*.xlsx;*.xlsm'
    if dialog.ShowDialog() != DialogResult.OK:
        return
    open_workbook(dialog.FileName)


def load_sheet(sender=None, args=None):
    """Silent no-op when there is nothing to load. SelectionChanged also fires
    while the sheet list is being rebuilt, so this must not alert."""
    if not xlsx_sheets or not window.sheets.SelectedItem:
        return
    name = str(window.sheets.SelectedItem)
    entry_name = None
    for sheet_name, sheet_entry in xlsx_sheets:
        if sheet_name == name:
            entry_name = sheet_entry
            break
    if entry_name is None:
        return
    busy(True)
    try:
        read_sheet(entry_name)
        window.excel_key.Items.Clear()
        window.match_list.Items.Clear()
        for display, _, _ in columns:
            window.excel_key.Items.Add(display)
        if window.excel_key.Items.Count:
            preferred = 0
            has_id_column = False
            for position, (display, header, _) in enumerate(columns):
                if header.lower() == 'element id':
                    preferred = position
                    has_id_column = True
                    break
            # Follow the sheet both ways. It used to stay ticked after
            # switching to a sheet with no Element Id column, so every row
            # was compared against element ids and nothing matched.
            window.match_by_id.IsChecked = has_id_column
            window.excel_key.SelectedIndex = preferred
        refresh_import_columns()
        window.summary.Text = '{0} data row(s) loaded from "{1}".'.format(len(sheet_data), name)
    except Exception as error:
        forms.alert(str(error), title=TITLE)
    finally:
        busy(False)


def matching_value(element):
    if window.match_by_id.IsChecked:
        return str(id_value(element.Id))
    parameter_name = window.revit_key.SelectedItem
    if not parameter_name:
        return None
    index = parameter_index(element, bool(window.allow_type_params.IsChecked))
    parameter, _ = resolve(index, str(parameter_name), bool(window.allow_type_params.IsChecked))
    return normalize_key(read_value(parameter, False))


def toggle_match_mode(sender=None, args=None):
    window.revit_key.IsEnabled = not window.match_by_id.IsChecked
    window.revit_key_label.Opacity = 0.4 if window.match_by_id.IsChecked else 1.0


def preview_item(text, color):
    item = ListBoxItem()
    item.Content = text
    item.Foreground = color
    window.match_list.Items.Add(item)


def preview(sender=None, args=None):
    window.match_list.Items.Clear()
    rows = excel_rows()
    if not rows or (not window.match_by_id.IsChecked and not window.revit_key.SelectedItem):
        window.summary.Text = 'Load a sheet and choose both matching columns.'
        return
    busy(True)
    try:
        # Matching elements are listed FIRST, then the ones with no Excel
        # row. They used to be mixed in model order, so on a large model the
        # matches could all sit below the display limit and never be seen.
        matched, unmatched, blank = [], [], 0
        for element in selected_elements():
            key = matching_value(element)
            if not key:
                blank += 1
                continue
            line = (key, str(id_value(element.Id)), family(element), type_name(element))
            (matched if key in rows else unmatched).append(line)

        def draw(lines, label, brush, limit):
            for key, element_id, family_name, type_text in lines[:limit]:
                preview_item('{0:<9} {1:<26} {2:<10} {3} : {4}'.format(
                    label, key[:26], element_id, family_name, type_text), brush)
            if len(lines) > limit:
                preview_item('... {0} more {1} element(s) not shown'.format(
                    len(lines) - limit, label.lower()), INFO_BRUSH)

        preview_item('{0} matching  |  {1} with no Excel row  |  {2} with no key value'.format(
            len(matched), len(unmatched), blank), INFO_BRUSH)
        preview_item('{0:<9} {1:<26} {2:<10} {3}'.format('', 'Match value', 'Id', 'Family : Type'), INFO_BRUSH)
        draw(matched, 'MATCH', MATCH_BRUSH, PREVIEW_LIMIT)
        draw(unmatched, 'NO MATCH', NO_MATCH_BRUSH, max(50, PREVIEW_LIMIT - len(matched)))
        window.summary.Text = '{0} match(es), {1} unmatched, {2} with no key value.'.format(
            len(matched), len(unmatched), blank)
        if active_rules:
            window.summary.Text += ' Parameter filter on.'
    finally:
        busy(False)


# ==========================================================================
# Import
# ==========================================================================
def worksharing_block(element):
    """Why this element cannot be edited right now, or None.

    In a workshared model, setting a parameter on an element borrowed by
    someone else (or changed in central since the last sync) is accepted by
    Parameter.Set but fails on commit - and that failure rolls back the
    WHOLE import transaction, so not a single value is written."""
    if WorksharingUtils is None or not doc.IsWorkshared:
        return None
    try:
        status = WorksharingUtils.GetCheckoutStatus(doc, element.Id)
        if status == CheckoutStatus.OwnedByOtherUser:
            try:
                owner = WorksharingUtils.GetWorksharingTooltipInfo(doc, element.Id).Owner
            except Exception:
                owner = 'another user'
            return 'borrowed by {0}'.format(owner or 'another user')
        updates = WorksharingUtils.GetModelUpdatesStatus(doc, element.Id)
        if updates in (ModelUpdatesStatus.UpdatedInCentral, ModelUpdatesStatus.DeletedInCentral):
            return 'changed in central - Synchronize first'
    except Exception:
        pass
    return None


def import_data(sender, args):
    global last_results
    rows = excel_rows()
    selected_columns = sorted(checked_labels(window.excel_columns))
    if not rows or (not window.match_by_id.IsChecked and not window.revit_key.SelectedItem) or not selected_columns:
        forms.alert('Load a sheet, set the matching columns, and tick at least one column to import.', title=TITLE)
        return

    if doc.IsReadOnly:
        forms.alert('This document is read-only, so nothing can be written.', title=TITLE)
        return

    targets = []
    for display in selected_columns:
        entry = column_by_display(display)
        if entry is not None:
            targets.append((entry[1], entry[2]))        # real parameter name, column index

    allow_type = bool(window.allow_type_params.IsChecked)
    elements = selected_elements()
    updated = unmatched = failures = blocked = 0
    skip_reasons = {}
    last_results = []

    busy(True)
    try:
        with revit.Transaction('Import Excel to Revit'):
            for element in elements:
                key = matching_value(element)
                row = rows.get(key) if key else None
                element_id_text = str(id_value(element.Id))
                if row is None:
                    if key:
                        unmatched += 1
                        last_results.append(['Not Matching', key, category_name(element), family(element),
                                             element_id_text, '', 'No Excel row found'])
                    continue
                block = worksharing_block(element)
                if block:
                    blocked += 1
                    reason = 'element ' + block.split(' by ')[0]
                    skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
                    last_results.append(['Skipped - Not Editable', key, category_name(element),
                                         family(element), element_id_text, '', block])
                    continue
                index = parameter_index(element, allow_type)
                count = 0
                updated_fields = []
                notes = []
                for name, position in targets:
                    if position >= len(row):
                        continue
                    cell = row[position]
                    parameter, scope = resolve(index, name, allow_type)
                    success, reason = write_value(parameter, cell)
                    if success:
                        count += 1
                        updated_fields.append(name + (' [type]' if scope == 'type' else ''))
                    elif cell not in (None, '') and reason != 'blank value':
                        failures += 1
                        skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
                        notes.append('{0}: {1}'.format(name, reason))
                if count:
                    updated += 1
                    last_results.append(['Updated', key, category_name(element), family(element), element_id_text,
                                         ', '.join(updated_fields), '; '.join(notes)])
                else:
                    last_results.append(['Matched - No Update', key, category_name(element), family(element),
                                         element_id_text, '',
                                         '; '.join(notes) if notes else 'No writable selected value'])
    except Exception as error:
        # A commit failure (or anything unexpected) rolls the whole
        # transaction back. Say so instead of letting the exception escape
        # the WPF event handler.
        busy(False)
        window.export_results.IsEnabled = len(last_results) > 0
        forms.alert('The import was rolled back - nothing was written.\n\n{0}\n\n'
                    'Use "Export Import Results" to see how far it got.'.format(error), title=TITLE)
        return
    finally:
        busy(False)

    reason_lines = ['{0}: {1}'.format(reason, count) for reason, count
                    in sorted(skip_reasons.items(), key=lambda item: -item[1])]
    output.print_md('## {0}\nUpdated: **{1}** | Unmatched: **{2}** | Not editable: **{3}** | '
                    'Fields skipped: **{4}**{5}'.format(
                        TITLE, updated, unmatched, blocked, failures,
                        '\n\nSkip reasons:\n' + '\n'.join('- ' + line for line in reason_lines)
                        if reason_lines else ''))
    window.export_results.IsEnabled = len(last_results) > 0
    preview()
    message = 'Updated: {0}\nUnmatched: {1}\nNot editable (worksharing): {2}\nSkipped fields: {3}'.format(
        updated, unmatched, blocked, failures)
    if reason_lines:
        message += '\n\nWhy fields were skipped:\n' + '\n'.join('  - ' + line for line in reason_lines)
    message += '\n\nUse "Export Import Results" for the full per-element breakdown.'
    forms.alert(message, title=TITLE)


# ==========================================================================
# Export
# ==========================================================================
def export_results(sender, args):
    if not last_results:
        forms.alert('Run an import first to create a results file.', title=TITLE)
        return
    path = save_path('Revit_Import_Results')
    if not path:
        return
    rows = [['Status', 'Match Value', 'Category', 'Family', 'Element Id', 'Updated Parameters', 'Notes']]
    rows.extend(last_results)
    write_table(path, 'Import Results', rows)
    forms.alert('Results written to:\n{0}'.format(path), title=TITLE)


def export_elements(sender, args):
    """Export the current working set with the ticked parameters. Element Id is
    always the first column so the file round-trips exactly."""
    elements = selected_elements()
    if not elements:
        forms.alert('No elements in the current selection to export.', title=TITLE)
        return
    names = sorted(checked_labels(window.export_params))
    if not names:
        forms.alert('Tick at least one parameter to export.', title=TITLE)
        return

    allow_type = bool(window.allow_type_params.IsChecked)
    with_units = bool(window.export_units.IsChecked)
    split = bool(window.split_by_category.IsChecked)

    path = save_path('Revit_MEP_Export')
    if not path:
        return

    header = ['Element Id', 'Category', 'Family', 'Type'] + names
    grouped = {}
    busy(True)
    try:
        for element in elements:
            index = parameter_index(element, allow_type)
            row = [str(id_value(element.Id)), category_name(element), family(element), type_name(element)]
            for name in names:
                parameter, _ = resolve(index, name, allow_type)
                value = read_value(parameter, with_units)
                row.append(value if value is not None else '')
            bucket = category_name(element) if split else 'Revit Data'
            grouped.setdefault(bucket, []).append(row)
    finally:
        busy(False)

    if path.lower().endswith('.csv'):
        rows = [header]
        for bucket in sorted(grouped):
            rows.extend(grouped[bucket])
        write_csv(path, rows)
    else:
        write_xlsx(path, [(bucket, [header] + grouped[bucket]) for bucket in sorted(grouped)])

    forms.alert('Exported {0} element(s) and {1} parameter(s) to:\n{2}\n\n'
                'Edit the file, then use Browse Excel File to import it back. Leave the '
                'Element Id column untouched and tick "Match using Element Id" for an exact round-trip.'
                .format(len(elements), len(names), path), title=TITLE)


IDENTITY_HEADERS = ['Element Id', 'Category', 'Family', 'Type']
SOURCE_HEADER = 'Source Schedule'

LAYOUT_SHEETS = 0        # one workbook, one sheet per schedule
LAYOUT_COMBINED = 1      # one workbook, every schedule on one sheet
LAYOUT_SEPARATE = 2      # one workbook per schedule


def schedule_data_table(schedule, allow_type, with_units):
    """Editable export of one schedule: one row per element, Element Id first.

    Returns (field names, rows, note). field names is None when the schedule
    cannot be exported this way; note then says why."""
    elements = schedule_elements(schedule)
    if not elements:
        return None, None, 'no elements'
    field_ids = {}
    names = []
    skipped = []
    for field in schedule_visible_fields(schedule):
        name = field_parameter_name(field)
        try:
            param_id = field.ParameterId
        except Exception:
            param_id = None
        if name and param_id is not None and param_id != ElementId.InvalidElementId:
            if name not in field_ids:
                field_ids[name] = param_id
                names.append(name)
        elif name:
            skipped.append(name)
    if not names:
        return None, None, 'only calculated / formula fields'
    rows = []
    for element in elements:
        index = parameter_index(element, allow_type)
        row = [str(id_value(element.Id)), category_name(element), family(element), type_name(element)]
        for name in names:
            parameter, _ = resolve(index, name, allow_type, field_ids)
            value = read_value(parameter, with_units)
            row.append(value if value is not None else '')
        rows.append(row)
    note = 'calculated field(s) left out: {0}'.format(', '.join(skipped)) if skipped else ''
    return names, rows, note


def schedule_display_rows(schedule):
    """The schedule exactly as Revit shows it: title, column headings,
    grouping rows, totals and calculated fields. Text only - a report copy."""
    from Autodesk.Revit.DB import SectionType
    table = schedule.GetTableData()
    rows = []
    for section_type in (SectionType.Header, SectionType.Body):
        try:
            section = table.GetSectionData(section_type)
        except Exception:
            section = None
        if section is None:
            continue
        first_row, first_column = section.FirstRowNumber, section.FirstColumnNumber
        for row_number in range(first_row, first_row + section.NumberOfRows):
            row = []
            for column_number in range(first_column, first_column + section.NumberOfColumns):
                try:
                    row.append(schedule.GetCellText(section_type, row_number, column_number) or '')
                except Exception:
                    row.append('')
            if any(row) or section_type == SectionType.Body:
                rows.append(row)
    return rows if any(any(row) for row in rows) else []


def table_rows(table):
    """(schedule name, field names or None, rows) -> rows for one worksheet."""
    _, names, rows = table
    if names is None:                      # 'as shown': already complete
        return rows
    return [IDENTITY_HEADERS + names] + rows


def combine_data_tables(tables):
    """Every schedule on one importable sheet: a Source Schedule column, the
    identity columns, then the union of all fields (first seen order)."""
    union = []
    for _, names, _ in tables:
        for name in names:
            if name not in union:
                union.append(name)
    position = dict((name, index) for index, name in enumerate(union))
    combined = [[SOURCE_HEADER] + IDENTITY_HEADERS + union]
    identity = len(IDENTITY_HEADERS)
    for schedule_name, names, rows in tables:
        for row in rows:
            values = [''] * len(union)
            for offset, name in enumerate(names):
                values[position[name]] = row[identity + offset]
            combined.append([schedule_name] + row[:identity] + values)
    return combined


def combine_display_tables(tables):
    """Report copies stacked on one sheet, a name row above each block."""
    combined = []
    for schedule_name, _, rows in tables:
        if combined:
            combined.append([])
        combined.append([schedule_name])
        combined.extend(rows)
    return combined


def export_schedule(sender, args):
    """Export every ticked schedule, laid out the way the user chose."""
    chosen = ticked_schedules()
    if not chosen:
        forms.alert('Tick at least one schedule to export.', title=TITLE)
        return
    layout = max(window.schedule_layout.SelectedIndex, 0)
    as_shown = window.schedule_content.SelectedIndex == 1
    many = len(chosen) > 1
    separate = many and layout == LAYOUT_SEPARATE
    combined = many and layout == LAYOUT_COMBINED

    # ---- where to save -----------------------------------------------------
    path = folder = None
    if separate:
        folder = pick_folder('Folder for the {0} schedule workbooks'.format(len(chosen)))
        if not folder:
            return
    else:
        default = chosen[0].Name if not many else '{0} - Schedules'.format(doc.Title)
        path = save_path(default, allow_csv=(not many or combined))
        if not path:
            return

    # ---- read the schedules (read-only, no transaction) ----------------------
    allow_type = bool(window.allow_type_params.IsChecked)
    with_units = bool(window.export_units.IsChecked)
    tables = []              # [(schedule name, field names or None, rows)]
    notes = []
    busy(True)
    try:
        for schedule in chosen:
            name = schedule.Name
            try:
                if as_shown:
                    rows = schedule_display_rows(schedule)
                    if rows:
                        tables.append((name, None, rows))
                    else:
                        notes.append('{0}: skipped - empty'.format(name))
                else:
                    names, rows, note = schedule_data_table(schedule, allow_type, with_units)
                    if names is None:
                        notes.append('{0}: skipped - {1}'.format(name, note))
                        continue
                    if note:
                        notes.append('{0}: {1}'.format(name, note))
                    tables.append((name, names, rows))
            except Exception as error:
                notes.append('{0}: skipped - {1}'.format(name, error))
    finally:
        busy(False)

    if not tables:
        forms.alert('Nothing was exported.\n\n' + '\n'.join(notes), title=TITLE)
        return

    # ---- write ---------------------------------------------------------------
    try:
        if separate:
            used = set()
            targets = [(os.path.join(folder, safe_file_name(table[0], used) + '.xlsx'), table)
                       for table in tables]
            existing = [target for target, _ in targets if File.Exists(target)]
            if existing and not forms.alert(
                    '{0} of these workbooks already exist in the folder and will be replaced. Continue?'
                    .format(len(existing)), title=TITLE, yes=True, no=True):
                return
            for target, table in targets:
                write_xlsx(target, [(table[0], table_rows(table))])
            destination = '{0} workbook(s) in:\n{1}'.format(len(targets), folder)
        elif combined:
            rows = combine_display_tables(tables) if as_shown else combine_data_tables(tables)
            write_table(path, 'Schedules', rows)
            destination = 'one sheet in:\n{0}'.format(path)
        elif path.lower().endswith('.csv'):
            write_csv(path, table_rows(tables[0]))
            destination = path
        else:
            write_xlsx(path, [(table[0], table_rows(table)) for table in tables])
            destination = '{0} sheet(s) in:\n{1}'.format(len(tables), path)
    except Exception as error:
        forms.alert('The file could not be written. If it is open in Excel, close it and try again.'
                    '\n\n{0}'.format(error), title=TITLE)
        return

    message = 'Exported {0} of {1} schedule(s) - {2}'.format(len(tables), len(chosen), destination)
    if as_shown:
        message += '\n\nThis is a report copy (as shown in Revit). It cannot be imported back.'
    else:
        message += ('\n\nEdit it, then use Browse Excel File and pick the worksheet to import it '
                    'back. The Element Id column is matched automatically.')
    if notes:
        message += '\n\nNotes:\n' + '\n'.join('  - ' + note for note in notes[:25])
        if len(notes) > 25:
            message += '\n  ... and {0} more'.format(len(notes) - 25)
    window.summary.Text = 'Exported {0} schedule(s).'.format(len(tables))
    forms.alert(message, title=TITLE)


# ==========================================================================
# Templates - the whole setup of this window, saved by name for next time
# ==========================================================================
TEMPLATE_FILE = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')),
                             'EleTools', 'ExcelRevitMEP', 'templates.json')


def read_template_store():
    """{'last': name or None, 'templates': {name: setup}}"""
    try:
        with open(TEMPLATE_FILE, 'r') as handle:
            store = json.load(handle)
        if isinstance(store, dict) and isinstance(store.get('templates'), dict):
            return store
    except Exception:
        pass
    return {'last': None, 'templates': {}}


def write_template_store(store):
    folder = os.path.dirname(TEMPLATE_FILE)
    if not os.path.isdir(folder):
        os.makedirs(folder)
    with open(TEMPLATE_FILE, 'w') as handle:
        json.dump(store, handle, indent=2, sort_keys=True)


def set_checked(listbox, labels):
    """Tick exactly the items named in `labels`. Returns the names not found."""
    wanted = set(labels or [])
    found = set()
    for item in listbox.Items:
        try:
            name = str(item.Tag)
        except AttributeError:
            continue
        item.IsChecked = name in wanted
        if name in wanted:
            found.add(name)
    return sorted(wanted - found)


def all_labels(listbox):
    labels = set()
    for item in listbox.Items:
        try:
            labels.add(str(item.Tag))
        except AttributeError:
            pass
    return labels


def capture_setup():
    """Everything chosen in the window, as plain JSON-friendly data."""
    families_ticked = checked_labels(window.families)
    return {
        'mode': 'schedule' if window.schedule_mode.IsChecked else 'category',
        'categories': sorted(checked_labels(window.categories)),
        'schedules': sorted(checked_labels(window.schedules)),
        # None = "all families": a family added to the model later is then included
        'families': None if families_ticked == all_labels(window.families) else sorted(families_ticked),
        'rules': [list(rule) for rule in rules_in_ui()],
        'allow_type': bool(window.allow_type_params.IsChecked),
        'export_units': bool(window.export_units.IsChecked),
        'split_by_category': bool(window.split_by_category.IsChecked),
        'export_params': sorted(checked_labels(window.export_params)),
        'schedule_layout': int(window.schedule_layout.SelectedIndex),
        'schedule_content': int(window.schedule_content.SelectedIndex),
        'match_by_id': bool(window.match_by_id.IsChecked),
        'revit_key': str(window.revit_key.SelectedItem) if window.revit_key.SelectedItem else None,
        'workbook': xlsx_path,
        'sheet': str(window.sheets.SelectedItem) if window.sheets.SelectedItem else None,
        'excel_key': str(window.excel_key.SelectedItem) if window.excel_key.SelectedItem else None,
        'import_columns': sorted(checked_labels(window.excel_columns)),
    }


def apply_setup(setup):
    """Put a saved setup back into the window. Returns notes about anything
    the template names that this model or workbook does not have."""
    global active_rules
    notes = []
    busy(True)
    try:
        # 1. source - set everything first, then query the model ONCE
        refresh_hold[0] = True
        try:
            window.allow_type_params.IsChecked = bool(setup.get('allow_type'))
            window.export_units.IsChecked = bool(setup.get('export_units'))
            window.split_by_category.IsChecked = bool(setup.get('split_by_category'))
            missing = set_checked(window.categories, setup.get('categories'))
            if missing:
                notes.append('Categories not in this model: ' + ', '.join(missing))
            missing = set_checked(window.schedules, setup.get('schedules'))
            if missing:
                notes.append('Schedules not in this model: ' + ', '.join(missing))
            if setup.get('mode') == 'schedule':
                window.schedule_mode.IsChecked = True
            else:
                window.category_mode.IsChecked = True
        finally:
            refresh_hold[0] = False
        active_rules = []
        window.families.Items.Clear()          # so every family starts ticked
        window.export_params.Items.Clear()
        refresh_source()

        # 2. families, filter, export parameters
        if setup.get('families') is not None:
            missing = set_checked(window.families, setup.get('families'))
            if missing:
                notes.append('Families not in this selection: ' + ', '.join(missing))
        rules = [tuple(rule) for rule in (setup.get('rules') or []) if len(rule) == 3]
        known = [str(item) for item in window.filter1_param.Items]
        for name, _, _ in rules:
            if name not in known:
                notes.append('Filter parameter not found on these elements: ' + name)
        set_rules_in_ui(rules)
        active_rules = rules_in_ui()
        missing = set_checked(window.export_params, setup.get('export_params'))
        if missing:
            notes.append('Export parameters not found: ' + ', '.join(missing))
        for box, key in ((window.schedule_layout, 'schedule_layout'),
                         (window.schedule_content, 'schedule_content')):
            index = setup.get(key)
            if isinstance(index, int) and 0 <= index < box.Items.Count:
                box.SelectedIndex = index

        # 3. workbook, worksheet, matching, import columns
        path = setup.get('workbook')
        if path:
            if File.Exists(path):
                if open_workbook(path, setup.get('sheet')):
                    if setup.get('sheet') and str(window.sheets.SelectedItem) != setup.get('sheet'):
                        notes.append('Worksheet "{0}" is not in the workbook; the first sheet was loaded.'
                                     .format(setup.get('sheet')))
                    key = setup.get('excel_key')
                    if key and key in [str(item) for item in window.excel_key.Items]:
                        window.excel_key.SelectedItem = key
                    elif key:
                        notes.append('Excel match column "{0}" is not in the worksheet.'.format(key))
                    if setup.get('import_columns'):
                        missing = set_checked(window.excel_columns, setup.get('import_columns'))
                        if missing:
                            notes.append('Import columns not in the worksheet: ' + ', '.join(missing))
            else:
                notes.append('Workbook not found: {0}'.format(path))
        # after the sheet: loading a sheet with an Element Id column ticks this itself
        window.match_by_id.IsChecked = bool(setup.get('match_by_id'))
        key = setup.get('revit_key')
        if key:
            if key in [str(item) for item in window.revit_key.Items]:
                window.revit_key.SelectedItem = key
            elif not setup.get('match_by_id'):
                notes.append('Revit matching parameter "{0}" not found.'.format(key))
        toggle_match_mode()
        update_filter_note()
        if xlsx_sheets and columns:
            preview()
    finally:
        busy(False)
    return notes


def refresh_template_list(select=None):
    store = read_template_store()
    window.templates.Items.Clear()
    for name in sorted(store['templates'], key=lambda item: item.lower()):
        window.templates.Items.Add(name)
    names = [str(item) for item in window.templates.Items]
    if select in names:
        window.templates.SelectedItem = select
    window.template_load.IsEnabled = window.template_delete.IsEnabled = bool(names)
    last = store.get('last')
    if not names:
        window.template_note.Text = 'No template yet - set the window up, then "Save current setup...".'
    elif last in names:
        window.template_note.Text = 'Last used: {0}'.format(last)
    else:
        window.template_note.Text = ''


def template_save(sender, args):
    current = str(window.templates.SelectedItem) if window.templates.SelectedItem else ''
    name = forms.ask_for_string(default=current, title=TITLE,
                                prompt='Name for this setup (an existing name is replaced):')
    name = (name or '').strip()
    if not name:
        return
    store = read_template_store()
    if name in store['templates'] and name != current and not forms.alert(
            'A template named "{0}" already exists. Replace it?'.format(name),
            title=TITLE, yes=True, no=True):
        return
    store['templates'][name] = capture_setup()
    store['last'] = name
    try:
        write_template_store(store)
    except Exception as error:
        forms.alert('The template could not be saved.\n\n{0}'.format(error), title=TITLE)
        return
    refresh_template_list(select=name)
    window.template_note.Text = 'Saved "{0}".'.format(name)


def template_load(sender, args):
    if not window.templates.SelectedItem:
        forms.alert('Choose a template in the list first.', title=TITLE)
        return
    name = str(window.templates.SelectedItem)
    store = read_template_store()
    setup = store['templates'].get(name)
    if setup is None:
        refresh_template_list()
        return
    notes = apply_setup(setup)
    store['last'] = name
    try:
        write_template_store(store)
    except Exception:
        pass
    window.template_note.Text = 'Loaded "{0}".'.format(name)
    if notes:
        forms.alert('Template "{0}" was loaded, with these differences:\n\n{1}'.format(
            name, '\n'.join('  - ' + note for note in notes)), title=TITLE)


def template_delete(sender, args):
    if not window.templates.SelectedItem:
        return
    name = str(window.templates.SelectedItem)
    if not forms.alert('Delete the template "{0}"?'.format(name), title=TITLE, yes=True, no=True):
        return
    store = read_template_store()
    store['templates'].pop(name, None)
    if store.get('last') == name:
        store['last'] = None
    try:
        write_template_store(store)
    except Exception as error:
        forms.alert('The template could not be deleted.\n\n{0}'.format(error), title=TITLE)
    refresh_template_list()


# ==========================================================================
# Wire-up
# ==========================================================================
window = forms.WPFWindow('ui.xaml')

for label, _ in CATEGORY_OPTIONS:
    window.categories.Items.Add(checkbox_item(label, False, refresh_categories))
ALL_SCHEDULES = schedule_options()
for schedule in ALL_SCHEDULES:
    window.schedules.Items.Add(checkbox_item(schedule.Name, False, refresh_schedule))
window.schedules_count.Text = '0 of {0} ticked'.format(len(ALL_SCHEDULES))

for _param_box, op_box, _value_box in rule_controls():
    for operator in FILTER_OPS:
        op_box.Items.Add(operator)
    op_box.SelectedIndex = 0

window.duplicate_note.Visibility = Visibility.Collapsed
toggle_match_mode()
refresh_categories()
refresh_template_list(select=read_template_store().get('last'))

def load_sheet_clicked(sender, args):
    if not xlsx_sheets or not window.sheets.SelectedItem:
        forms.alert('Select an Excel workbook and worksheet first.', title=TITLE)
        return
    load_sheet()


window.template_load.Click += template_load
window.template_save.Click += template_save
window.template_delete.Click += template_delete
window.filter_apply.Click += apply_filter
window.filter_clear.Click += clear_filter
window.filter1_value.KeyDown += filter_enter
window.filter2_value.KeyDown += filter_enter

window.browse.Click += browse
window.load_sheet.Click += load_sheet_clicked
window.sheets.SelectionChanged += load_sheet
window.excel_key.SelectionChanged += refresh_import_columns

window.categories_all.Click += lambda sender, args: tick_many(window.categories, True, refresh_categories)
window.categories_none.Click += lambda sender, args: tick_many(window.categories, False, refresh_categories)
window.schedules_all.Click += lambda sender, args: tick_many(window.schedules, True, refresh_schedule)
window.schedules_none.Click += lambda sender, args: tick_many(window.schedules, False, refresh_schedule)
window.schedules_search.TextChanged += lambda sender, args: filter_listbox(window.schedules, window.schedules_search.Text)
window.families_all.Click += lambda sender, args: check_all(window.families)
window.families_none.Click += lambda sender, args: check_none(window.families)
window.export_params_all.Click += lambda sender, args: check_all(window.export_params)
window.export_params_none.Click += lambda sender, args: check_none(window.export_params)
window.excel_columns_all.Click += lambda sender, args: check_all(window.excel_columns)
window.excel_columns_none.Click += lambda sender, args: check_none(window.excel_columns)

window.categories_search.TextChanged += lambda sender, args: filter_listbox(window.categories, window.categories_search.Text)
window.families_search.TextChanged += lambda sender, args: filter_listbox(window.families, window.families_search.Text)
window.export_params_search.TextChanged += lambda sender, args: filter_listbox(window.export_params, window.export_params_search.Text)
window.excel_columns_search.TextChanged += lambda sender, args: filter_listbox(window.excel_columns, window.excel_columns_search.Text)

window.category_mode.Checked += set_mode_category
window.schedule_mode.Checked += set_mode_schedule
window.allow_type_params.Checked += refresh_source
window.allow_type_params.Unchecked += refresh_source

window.export_schedule.Click += export_schedule
window.export_elements.Click += export_elements
window.match_by_id.Checked += toggle_match_mode
window.match_by_id.Unchecked += toggle_match_mode
window.preview.Click += preview
window.import_data.Click += import_data
window.export_results.Click += export_results
window.cancel.Click += lambda sender, args: window.Close()

window.ShowDialog()
