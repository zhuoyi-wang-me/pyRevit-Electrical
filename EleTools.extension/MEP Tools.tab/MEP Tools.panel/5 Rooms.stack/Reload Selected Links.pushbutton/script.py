# -*- coding: utf-8 -*-
# =============================================================================
#  Reload Selected Links  -  pyRevit pushbutton
# -----------------------------------------------------------------------------
#  Lists every Revit link (RevitLinkType) in the active project, lets the user
#  tick any number of them and reloads the ticked links one after another -
#  the same operation as Manage Links > Reload - without restarting Revit.
#
#  Revit API facts this tool relies on (same in Revit 2022 - 2026):
#    * RevitLinkType.Load() loads or reloads a link from its stored location
#      and returns a LinkLoadResult; LinkLoadResultType.LinkLoaded = success.
#      RevitLinkType.Reload() is documented as identical to Load().
#    * Load() regenerates the model and clears the Undo history. It throws if
#      any Transaction, SubTransaction or TransactionGroup is open, so this
#      script never opens one.
#    * Nested links are not top-level links and are rejected by Load(); they
#      reload together with their parent link.
#    * A link that is "Unloaded for me" is restored with
#      RevertLocalUnloadStatus(), which reloads it for the current user.
#    * The dialog is modal (ShowDialog), so every API call runs inside the
#      valid API context of this pyRevit command; no ExternalEvent is needed.
#    * ElementId.IntegerValue was deprecated in 2024 and removed in 2026;
#      element_id_value() reads ElementId.Value where available.
#
#  Compatible with pyRevit's IronPython engines (2.7.12 default, 3.4).
# =============================================================================

import os
import time
import traceback

import clr
clr.AddReference('PresentationCore')
clr.AddReference('PresentationFramework')
clr.AddReference('WindowsBase')
# .NET 8 (Revit 2025+) moved INotifyPropertyChanged / ObservableCollection into
# System.ObjectModel; load it explicitly. Harmless to skip on .NET Framework.
for _asm in ('System.ObjectModel', 'System.Runtime', 'System'):
    try:
        clr.AddReference(_asm)
    except Exception:
        pass

from System import Action, Predicate
from System.Collections.ObjectModel import ObservableCollection
from System.ComponentModel import INotifyPropertyChanged, PropertyChangedEventArgs
from System.Windows import MessageBox, MessageBoxButton, MessageBoxImage
from System.Windows import Visibility
from System.Windows.Data import CollectionViewSource
from System.Windows.Input import Cursors, Key
from System.Windows.Media import BrushConverter
from System.Windows.Threading import DispatcherPriority

from Autodesk.Revit.DB import (
    CheckoutStatus,
    Element,
    ElementId,
    FilteredElementCollector,
    ModelPathUtils,
    RevitLinkInstance,
    RevitLinkType,
    WorksharingUtils,
)

from pyrevit import forms, revit, script


logger = script.get_logger()


# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------
TOOL_TITLE = 'Reload Selected Links'
XAML_FILE = 'ReloadLinks.xaml'
ICON_FILE = 'icon.png'
MAX_LINKS_LISTED_IN_SUMMARY = 12
NESTED_PREFIX = u'\u21b3 '          # "turn down-right" arrow shown before nested links

# LinkedFileStatus member names (identical in Revit 2022 - 2026).
STATUS_LOADED = 'Loaded'
STATUS_UNLOADED = 'Unloaded'
STATUS_NOT_FOUND = 'NotFound'
STATUS_CLOSED_WORKSET = 'InClosedWorkset'
STATUS_LOCALLY_UNLOADED = 'LocallyUnloaded'
STATUS_UNKNOWN = 'Unknown'
STATUS_NESTED = 'Nested'            # display-only status used by this tool

STATUS_LABELS = {
    STATUS_LOADED: 'Loaded',
    STATUS_UNLOADED: 'Unloaded',
    STATUS_NOT_FOUND: 'Not Found',
    STATUS_CLOSED_WORKSET: 'Closed Workset',
    STATUS_LOCALLY_UNLOADED: 'Unloaded for Me',
    STATUS_NESTED: 'Nested',
    STATUS_UNKNOWN: 'Unknown',
    'Imported': 'Imported',
    'CanBeUpgraded': 'Can Be Upgraded',
    'Invalid': 'Invalid',
}

# LinkLoadResultType member names. Only LinkLoaded means the link is loaded.
RESULT_LINK_LOADED = 'LinkLoaded'
LOAD_RESULT_MESSAGES = {
    'LinkNotFound': 'The linked file could not be found at its saved path.',
    'LinkNotOpenable': ('Revit could not read the linked file. It may be damaged, '
                        'not a Revit model, or saved in a newer Revit version.'),
    'LinkOpenAsHost': ('The linked model is open directly in this Revit session. '
                       'Close it, then reload.'),
    'SameModelAsHost': 'The link points to the model that is currently open.',
    'SameCentralModelAsHost': 'The link and this model belong to the same central model.',
    'LinkNotLoadedOtherError': 'Revit reported an unspecified error while loading the link.',
    'ExternalServerMissing': ('The add-in (external resource server) required to load '
                              'this link is not available.'),
    'LinkExists': 'Revit already has a link to this location.',
    'Uninitialized': 'Revit did not attempt to load the link.',
}

# Extra guidance for exceptions whose messages are terse.
EXCEPTION_HINTS = {
    'FileAccessException': 'Check that you have read access to the file and its folder.',
    'RevitServerUnauthorizedException': ('Your Autodesk account does not have access to '
                                         'this cloud model.'),
    'RevitServerUnauthenticatedUserException': 'Sign in to your Autodesk account, then try again.',
}

# Where a link is stored. Shown in the "Source" column.
SOURCE_LOCAL = 'Local drive'
SOURCE_NETWORK = 'Network share'
SOURCE_SERVER = 'Revit Server'
SOURCE_CLOUD = 'Cloud (ACC/BIM 360)'
SOURCE_DESKTOP_CONNECTOR = 'Desktop Connector'
SOURCE_UNKNOWN = 'Unknown'
FILE_SYSTEM_SOURCES = (SOURCE_LOCAL, SOURCE_NETWORK, SOURCE_DESKTOP_CONNECTOR)
CLOUD_PATH_PREFIXES = ('bim 360:', 'autodesk docs:', 'cloud:')


# -----------------------------------------------------------------------------
# Brushes (frozen so WPF can share them freely)
# -----------------------------------------------------------------------------
_BRUSH_CONVERTER = BrushConverter()


def make_brush(hex_color):
    """Create a frozen SolidColorBrush from '#RRGGBB'."""
    brush = _BRUSH_CONVERTER.ConvertFromString(hex_color)
    brush.Freeze()
    return brush


BRUSH_TEXT = make_brush('#1F2933')
BRUSH_MUTED = make_brush('#6B7480')
BRUSH_OK = make_brush('#1E7B34')
BRUSH_WARN = make_brush('#9A5B00')
BRUSH_ERROR = make_brush('#B42318')
BRUSH_ACTIVE = make_brush('#1B6AC9')

STATUS_BRUSHES = {
    STATUS_LOADED: BRUSH_OK,
    STATUS_UNLOADED: BRUSH_MUTED,
    STATUS_NOT_FOUND: BRUSH_ERROR,
    STATUS_CLOSED_WORKSET: BRUSH_WARN,
    STATUS_LOCALLY_UNLOADED: BRUSH_WARN,
    STATUS_NESTED: BRUSH_MUTED,
}


# -----------------------------------------------------------------------------
# Small helpers
# -----------------------------------------------------------------------------
def to_text(value, default=''):
    """Convert .NET / Python values to display text without ever raising."""
    if value is None:
        return default
    try:
        return value if isinstance(value, str) else str(value)
    except Exception:
        return default


def bundle_file(file_name):
    """Full path of a file stored next to this script."""
    try:
        bundle_dir = os.path.dirname(os.path.abspath(__file__))
    except NameError:
        bundle_dir = script.get_script_path()
    return os.path.join(bundle_dir, file_name)


def element_id_value(element_id):
    """Numeric value of an ElementId on every supported Revit version.

    Revit 2024+ exposes the 64-bit ElementId.Value. IntegerValue was
    deprecated in 2024 and is no longer available in Revit 2026.
    """
    try:
        return element_id.Value
    except AttributeError:
        return element_id.IntegerValue


def get_element_name(element):
    """Element name, tolerant of IronPython's Element.Name quirks."""
    getters = (
        lambda e: e.Name,
        lambda e: Element.Name.GetValue(e),
        lambda e: Element.Name.__get__(e),
    )
    for getter in getters:
        try:
            name = to_text(getter(element))
            if name:
                return name
        except Exception:
            continue
    return '<unnamed link>'


def short_name(path_text):
    """File name part of a local, UNC or cloud path."""
    text = to_text(path_text).replace('\\', '/').rstrip('/')
    return text.rsplit('/', 1)[-1] or to_text(path_text)


def status_label(status_key):
    return STATUS_LABELS.get(status_key, status_key or STATUS_LABELS[STATUS_UNKNOWN])


def format_duration(seconds):
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return '{0:.1f} s'.format(seconds)
    minutes, secs = divmod(int(round(seconds)), 60)
    return '{0} min {1:02d} s'.format(minutes, secs)


def plural(count, word):
    return '{0} {1}{2}'.format(count, word, '' if count == 1 else 's')


def dispose_quietly(api_object):
    """Release a disposable API object (LinkLoadResult implements IDisposable)."""
    if api_object is None:
        return
    try:
        api_object.Dispose()
    except Exception:
        pass


def file_exists(path_text):
    """True when a file-system path points to a readable file.

    Unknown situations return True so the tool never claims a file is missing
    without evidence.
    """
    try:
        return os.path.isfile(path_text)
    except Exception:
        return True


def describe_exception(error):
    """Readable 'ExceptionType: message' text for Python and .NET exceptions."""
    net_error = getattr(error, 'clsException', None)
    source = net_error if net_error is not None else error
    type_name = type(error).__name__
    if net_error is not None:
        try:
            type_name = to_text(net_error.GetType().Name) or type_name
        except Exception:
            pass
    message = ''
    for attribute in ('Message', 'message'):
        try:
            message = to_text(getattr(source, attribute))
        except Exception:
            message = ''
        if message:
            break
    if not message:
        message = to_text(error)
    message = ' '.join(message.split()) or 'No details were provided.'
    text = '{0}: {1}'.format(type_name, message)
    hint = EXCEPTION_HINTS.get(type_name)
    return '{0} {1}'.format(text, hint) if hint else text


# -----------------------------------------------------------------------------
# Reading links from the model
# -----------------------------------------------------------------------------
def get_document_blocker(doc):
    """Reason why links cannot be reloaded in this document, or None."""
    try:
        if doc is None or not doc.IsValidObject:
            return 'There is no active Revit document. Open a project and run the tool again.'
        if doc.IsFamilyDocument:
            return 'Revit links exist only in project files. Open a project and run the tool again.'
        if doc.IsReadOnly:
            return 'This model is read-only right now, so Revit cannot reload its links.'
        if doc.IsModifiable:
            return ('A transaction is still open in this model. Revit only reloads links when '
                    'no transaction, sub-transaction or transaction group is active.')
    except Exception as error:
        return 'The active document could not be checked. {0}'.format(describe_exception(error))
    return None


def count_link_types(doc):
    """Number of RevitLinkType elements, or -1 when it cannot be determined."""
    try:
        return FilteredElementCollector(doc).OfClass(RevitLinkType).GetElementCount()
    except Exception:
        logger.debug('Could not count link types:\n%s', traceback.format_exc())
        return -1


def get_status_key(doc, link_type):
    """LinkedFileStatus member name of a link type, e.g. 'Loaded' or 'NotFound'."""
    try:
        return to_text(link_type.GetLinkedFileStatus(), STATUS_UNKNOWN)
    except Exception:
        pass
    try:
        return STATUS_LOADED if RevitLinkType.IsLoaded(doc, link_type.Id) else STATUS_UNLOADED
    except Exception:
        return STATUS_UNKNOWN


def get_link_path(link_type):
    """(user-visible path, ModelPath or None) for a link type."""
    try:
        reference = link_type.GetExternalFileReference()
    except Exception:
        reference = None
    if reference is None:
        return '', None
    for getter_name in ('GetAbsolutePath', 'GetPath'):
        try:
            model_path = getattr(reference, getter_name)()
            if model_path is None or model_path.Empty:
                continue
            visible_path = to_text(ModelPathUtils.ConvertModelPathToUserVisiblePath(model_path))
            if visible_path:
                return visible_path, model_path
        except Exception:
            continue
    return '', None


def classify_source(link_type, model_path, path_text):
    """Where the link is stored: local, network, Revit Server, cloud, ..."""
    try:
        if model_path is not None and model_path.CloudPath:
            return SOURCE_CLOUD
    except Exception:
        pass
    try:
        if model_path is not None and model_path.ServerPath:
            return SOURCE_SERVER
    except Exception:
        pass
    try:
        if link_type.IsFromRevitServer():
            return SOURCE_SERVER
    except Exception:
        pass

    lowered = to_text(path_text).strip().lower().replace('/', '\\')
    if not lowered:
        return SOURCE_UNKNOWN
    if lowered.startswith(CLOUD_PATH_PREFIXES):
        return SOURCE_CLOUD
    if lowered.startswith('rsn:'):
        return SOURCE_SERVER
    if '\\dc\\accdocs\\' in lowered:
        return SOURCE_DESKTOP_CONNECTOR
    if lowered.startswith('\\\\'):
        return SOURCE_NETWORK
    return SOURCE_LOCAL


def is_nested_link(link_type):
    try:
        return bool(link_type.IsNestedLink)
    except Exception:
        return False


def get_related_link_name(doc, link_type, method_name):
    """Name of the parent ('GetParentId') or top-level ('GetRootId') link."""
    try:
        related_id = getattr(link_type, method_name)()
        if related_id is None or related_id == ElementId.InvalidElementId:
            return ''
        related = doc.GetElement(related_id)
        return get_element_name(related) if related is not None else ''
    except Exception:
        return ''


def count_link_instances(doc):
    """{link type id text: number of RevitLinkInstance elements}."""
    counts = {}
    try:
        for instance in FilteredElementCollector(doc).OfClass(RevitLinkInstance):
            try:
                key = to_text(instance.GetTypeId())
                counts[key] = counts.get(key, 0) + 1
            except Exception:
                continue
    except Exception:
        logger.debug('Could not count link instances:\n%s', traceback.format_exc())
    return counts


def read_link_record(doc, link_type, instance_counts):
    """Plain-data snapshot of one RevitLinkType (one row in the dialog)."""
    name = get_element_name(link_type)
    nested = is_nested_link(link_type)
    real_status = get_status_key(doc, link_type)
    path_text, model_path = get_link_path(link_type)
    parent_name = get_related_link_name(doc, link_type, 'GetParentId') if nested else ''
    root_name = get_related_link_name(doc, link_type, 'GetRootId') if nested else ''
    group_name = (root_name or parent_name or name).lower()
    return {
        'type_id': link_type.Id,
        'unique_id': to_text(link_type.UniqueId),
        'id_text': to_text(element_id_value(link_type.Id)),
        'name': name,
        'nested': nested,
        'parent_name': parent_name,
        'real_status': real_status,
        'status_key': STATUS_NESTED if nested else real_status,
        'path': path_text,
        'source': classify_source(link_type, model_path, path_text),
        'instances': instance_counts.get(to_text(link_type.Id), 0),
        # Top-level links alphabetically; nested links directly under their top-level link.
        'sort_key': (group_name, 1 if nested else 0, name.lower()),
    }


def read_link_records(doc):
    """One record per RevitLinkType. Several instances of one type = one row."""
    instance_counts = count_link_instances(doc)
    records = []
    for link_type in FilteredElementCollector(doc).OfClass(RevitLinkType).ToElements():
        try:
            records.append(read_link_record(doc, link_type, instance_counts))
        except Exception:
            logger.debug('Skipped a link type that could not be read:\n%s', traceback.format_exc())
    records.sort(key=lambda record: record['sort_key'])
    return records


def get_other_owner(doc, element_id):
    """Name of another user who has borrowed the element, else ''."""
    try:
        if not doc.IsWorkshared:
            return ''
        if WorksharingUtils.GetCheckoutStatus(doc, element_id) != CheckoutStatus.OwnedByOtherUser:
            return ''
        owner = WorksharingUtils.GetWorksharingTooltipInfo(doc, element_id).Owner
        return to_text(owner) or 'another user'
    except Exception:
        return ''


# -----------------------------------------------------------------------------
# Reloading
# -----------------------------------------------------------------------------
class ReloadOutcome(object):
    """Result of reloading one link."""

    def __init__(self, unique_id, name):
        self.unique_id = unique_id
        self.name = name
        self.success = False
        self.message = ''
        self.warning = ''
        self.api_call = ''
        self.result_code = ''
        self.seconds = 0.0


def read_load_result_code(load_result):
    """LinkLoadResultType name from a LinkLoadResult, or '' if unavailable."""
    if load_result is None:
        return ''
    try:
        return to_text(load_result.LoadResult)
    except Exception:
        return ''


def describe_load_result(code):
    if not code:
        return 'Revit returned no load result and the link is not loaded.'
    explanation = LOAD_RESULT_MESSAGES.get(code)
    if explanation:
        return '{0} ({1})'.format(explanation, code)
    return 'Revit could not load the link ({0}).'.format(code)


def describe_nested_failures(load_result):
    """Nested links that did not load while their parent did, as one line."""
    if load_result is None:
        return ''
    try:
        nested_results = load_result.GetNestedLinkLoadResults()
        keys = list(nested_results.Keys)
    except Exception:
        return ''
    problems = []
    for key in keys:
        try:
            code = to_text(nested_results[key].LoadResult)
        except Exception:
            continue
        if code and code != RESULT_LINK_LOADED:
            problems.append('{0} ({1})'.format(short_name(key), code))
    if not problems:
        return ''
    label = 'Nested link not loaded' if len(problems) == 1 else 'Nested links not loaded'
    return '{0}: {1}'.format(label, ', '.join(problems))


def _reload_top_level_link(doc, link_type, status_before, outcome):
    """Call the API that matches Manage Links > Reload and judge the result."""
    type_id = link_type.Id
    api_calls = []

    # "Unload for me": removing the per-user override performs a full reload
    # when the link is loaded for the other users of the central model.
    if status_before == STATUS_LOCALLY_UNLOADED and hasattr(link_type, 'RevertLocalUnloadStatus'):
        api_calls.append('RevitLinkType.RevertLocalUnloadStatus()')
        outcome.api_call = ' then '.join(api_calls)
        if to_text(link_type.RevertLocalUnloadStatus()) == STATUS_LOADED:
            outcome.success = True
            outcome.message = 'Reloaded. The "Unload for me" setting was removed.'
            return
        link_type = doc.GetElement(type_id)

    # Load() == Reload(): reload from the currently stored path. Never call it
    # inside a transaction; Revit manages its own transactions here.
    api_calls.append('RevitLinkType.Load()')
    outcome.api_call = ' then '.join(api_calls)
    load_result = link_type.Load()
    try:
        outcome.result_code = read_load_result_code(load_result)
        outcome.warning = describe_nested_failures(load_result)
    finally:
        dispose_quietly(load_result)

    refreshed = doc.GetElement(type_id)
    loaded_now = (isinstance(refreshed, RevitLinkType)
                  and get_status_key(doc, refreshed) == STATUS_LOADED)

    if outcome.result_code == RESULT_LINK_LOADED and loaded_now:
        outcome.success = True
        outcome.message = 'Reloaded.'
    elif outcome.result_code == RESULT_LINK_LOADED:
        outcome.message = 'Revit reported success, but the link is still not loaded.'
    elif not outcome.result_code and loaded_now:
        outcome.success = True
        outcome.message = 'Reloaded.'
    else:
        outcome.message = describe_load_result(outcome.result_code)


def _append_failure_hints(doc, type_id, status_before, outcome):
    """Add practical next steps to a failure message. Never raises."""
    try:
        hints = []
        if status_before == STATUS_CLOSED_WORKSET:
            hints.append('Open the workset that holds this link (Collaborate > Worksets), '
                         'then reload.')
        link_type = doc.GetElement(type_id)
        if isinstance(link_type, RevitLinkType):
            path_text, model_path = get_link_path(link_type)
            source = classify_source(link_type, model_path, path_text)
            if source in FILE_SYSTEM_SOURCES and path_text and not file_exists(path_text):
                hints.append('No file is reachable at "{0}". Check the path, the drive mapping '
                             'and your folder permissions.'.format(path_text))
            elif source == SOURCE_CLOUD:
                hints.append('For cloud links, confirm you are signed in to your Autodesk '
                             'account and still have access to the project.')
        owner = get_other_owner(doc, type_id)
        if owner:
            hints.append('Note: the link type is currently borrowed by {0}.'.format(owner))
        if hints:
            outcome.message = ' '.join([outcome.message] + hints).strip()
    except Exception:
        logger.debug('Could not build failure hints:\n%s', traceback.format_exc())


def reload_link(doc, type_id, unique_id, display_name):
    """Reload one link like Manage Links > Reload.

    Never raises: every problem is captured in the returned ReloadOutcome so
    the batch always continues with the next link.
    """
    outcome = ReloadOutcome(unique_id, display_name)
    started = time.time()
    status_before = STATUS_UNKNOWN
    try:
        link_type = doc.GetElement(type_id)
        if not isinstance(link_type, RevitLinkType):
            outcome.message = 'The link no longer exists in this model.'
        elif is_nested_link(link_type):
            outcome.message = ('This is a nested link. Nested links are reloaded together '
                               'with their parent link.')
        else:
            status_before = get_status_key(doc, link_type)
            _reload_top_level_link(doc, link_type, status_before, outcome)
    except Exception as error:
        outcome.success = False
        outcome.message = describe_exception(error)
        logger.debug('Reloading "%s" raised:\n%s', display_name, traceback.format_exc())
    outcome.seconds = time.time() - started
    if not outcome.success:
        _append_failure_hints(doc, type_id, status_before, outcome)
    return outcome


class BatchSummary(object):
    """Counts and text for the end-of-run report."""

    def __init__(self, outcomes, not_processed_names, seconds):
        self.succeeded = [outcome for outcome in outcomes if outcome.success]
        self.failed = [outcome for outcome in outcomes if not outcome.success]
        self.warned = [outcome for outcome in self.succeeded if outcome.warning]
        self.not_processed = list(not_processed_names)
        self.seconds = seconds

    def status_line(self):
        parts = ['{0} reloaded'.format(len(self.succeeded)),
                 '{0} failed'.format(len(self.failed))]
        if self.not_processed:
            parts.append('{0} not processed'.format(len(self.not_processed)))
        return 'Finished in {0}: {1}.'.format(format_duration(self.seconds), ', '.join(parts))

    def _list_lines(self, title, entries):
        lines = ['', title]
        for name, text in entries[:MAX_LINKS_LISTED_IN_SUMMARY]:
            lines.append(u'\u2022 {0}: {1}'.format(name, text))
        hidden = len(entries) - MAX_LINKS_LISTED_IN_SUMMARY
        if hidden > 0:
            lines.append('...and {0} more. See the Result column.'.format(hidden))
        return lines

    def details(self):
        lines = ['Reloaded: {0}'.format(len(self.succeeded)),
                 'Failed: {0}'.format(len(self.failed))]
        if self.not_processed:
            lines.append('Not processed (stopped): {0}'.format(len(self.not_processed)))
        lines.append('Time: {0}'.format(format_duration(self.seconds)))
        if self.failed:
            lines.extend(self._list_lines(
                'Failed links:', [(o.name, o.message) for o in self.failed]))
        if self.warned:
            lines.extend(self._list_lines(
                'Reloaded with warnings:', [(o.name, o.warning) for o in self.warned]))
        if self.not_processed:
            lines.extend(self._list_lines(
                'Not processed:', [(name, 'stopped before this link') for name in self.not_processed]))
        if self.failed or self.not_processed:
            lines.extend(['', 'Those links are still ticked, so you can fix the cause '
                              'and click Reload Selected again.'])
        return '\n'.join(lines)


# -----------------------------------------------------------------------------
# View-model for the link list
# -----------------------------------------------------------------------------
class NotifyPropertyChangedBase(INotifyPropertyChanged):
    """Minimal INotifyPropertyChanged for WPF data binding in IronPython.

    IronPython maps the interface event to add_/remove_PropertyChanged.
    Handlers are stored per instance.
    """

    def __init__(self):
        self._property_changed_handlers = []

    def add_PropertyChanged(self, handler):
        self._property_changed_handlers.append(handler)

    def remove_PropertyChanged(self, handler):
        if handler in self._property_changed_handlers:
            self._property_changed_handlers.remove(handler)

    def OnPropertyChanged(self, property_name):
        args = PropertyChangedEventArgs(property_name)
        for handler in list(self._property_changed_handlers):
            handler(self, args)


class LinkItem(NotifyPropertyChangedBase):
    """One row of the dialog: one RevitLinkType."""

    def __init__(self, record, on_checked_changed):
        NotifyPropertyChangedBase.__init__(self)
        self._record = record
        self._on_checked_changed = on_checked_changed
        self._is_checked = False
        self._locked = False
        self._result_text = ''
        self._result_brush = BRUSH_MUTED
        self._result_tooltip = None

    # identity ---------------------------------------------------------------
    @property
    def type_id(self):
        return self._record['type_id']

    @property
    def unique_id(self):
        return self._record['unique_id']

    @property
    def name(self):
        return self._record['name']

    @property
    def is_selectable(self):
        return not self._record['nested']

    # check box --------------------------------------------------------------
    @property
    def is_checked(self):
        return self._is_checked

    @is_checked.setter
    def is_checked(self, value):
        new_value = bool(value) and self.is_selectable
        if new_value == self._is_checked:
            return
        self._is_checked = new_value
        self.OnPropertyChanged('is_checked')
        if self._on_checked_changed is not None:
            self._on_checked_changed()

    @property
    def can_toggle(self):
        return self.is_selectable and not self._locked

    def set_locked(self, locked):
        if locked != self._locked:
            self._locked = locked
            self.OnPropertyChanged('can_toggle')

    # display columns ----------------------------------------------------------
    @property
    def display_name(self):
        return NESTED_PREFIX + self.name if self._record['nested'] else self.name

    @property
    def name_brush(self):
        return BRUSH_MUTED if self._record['nested'] else BRUSH_TEXT

    @property
    def name_tooltip(self):
        lines = [self.name]
        if self._record['nested']:
            parent = self._record['parent_name'] or 'another link'
            lines.append('Nested in {0}. Nested links reload together with their '
                         'parent link.'.format(parent))
            lines.append('Current status: {0}'.format(status_label(self._record['real_status'])))
        lines.append('Element ID: {0}'.format(self._record['id_text']))
        return '\n'.join(lines)

    @property
    def status(self):
        return status_label(self._record['status_key'])

    @property
    def status_brush(self):
        return STATUS_BRUSHES.get(self._record['status_key'], BRUSH_MUTED)

    @property
    def source(self):
        return self._record['source']

    @property
    def instances(self):
        return to_text(self._record['instances'])

    @property
    def path(self):
        return self._record['path'] or 'Path not available'

    @property
    def path_tooltip(self):
        return self._record['path'] or None

    @property
    def result(self):
        return self._result_text

    @property
    def result_brush(self):
        return self._result_brush

    @property
    def result_tooltip(self):
        return self._result_tooltip

    # result handling ----------------------------------------------------------
    def set_result(self, text, brush, tooltip=None):
        self._result_text = text
        self._result_brush = brush
        self._result_tooltip = tooltip or None
        for property_name in ('result', 'result_brush', 'result_tooltip'):
            self.OnPropertyChanged(property_name)

    def get_result_state(self):
        if not self._result_text:
            return None
        return self._result_text, self._result_brush, self._result_tooltip

    def show_outcome(self, outcome):
        duration = format_duration(outcome.seconds)
        if outcome.success and outcome.warning:
            text, brush = 'Reloaded with warnings ({0})'.format(duration), BRUSH_WARN
        elif outcome.success:
            text, brush = 'Reloaded ({0})'.format(duration), BRUSH_OK
        else:
            text, brush = 'Failed: {0}'.format(outcome.message), BRUSH_ERROR
        details = [outcome.message]
        if outcome.success and outcome.warning:
            details.append(outcome.warning)
        details.append('API call: {0}'.format(outcome.api_call or 'none'))
        if outcome.result_code:
            details.append('Result code: {0}'.format(outcome.result_code))
        details.append('Time: {0}'.format(duration))
        self.set_result(text, brush, '\n'.join(details))


# -----------------------------------------------------------------------------
# Dialog
# -----------------------------------------------------------------------------
def _noop():
    pass


def ui_event(handler):
    """Decorator for WPF event handlers: exceptions never escape to Revit."""
    def wrapper(self, sender, args):
        try:
            handler(self, sender, args)
        except Exception as error:
            self._handle_unexpected_error(error)
    wrapper.__name__ = handler.__name__
    wrapper.__doc__ = handler.__doc__
    return wrapper


class ReloadLinksWindow(forms.WPFWindow):
    """Modal dialog that lists the links and runs the reload batch."""

    def __init__(self, xaml_path, doc):
        forms.WPFWindow.__init__(self, xaml_path)
        self._doc = doc
        self._busy = False
        self._stop_requested = False
        self._search_tokens = []
        self._link_items = ObservableCollection[object]()
        self.links_grid.ItemsSource = self._link_items
        # The grid shows a filtered view of the collection; the collection
        # itself always holds every link, so ticks survive any search.
        self._link_view = CollectionViewSource.GetDefaultView(self._link_items)
        self._filter_delegate = Predicate[object](self._matches_search)
        self._link_view.Filter = self._filter_delegate
        self._apply_bundle_icon()

        self.select_all_button.Click += self._on_select_all
        self.clear_all_button.Click += self._on_clear_all
        self.reload_button.Click += self._on_reload
        self.close_button.Click += self._on_close
        self.search_box.TextChanged += self._on_search_changed
        self.search_box.PreviewKeyDown += self._on_search_key
        self.search_clear_button.Click += self._on_search_clear
        self.Loaded += self._on_loaded
        self.Closing += self._on_closing

        self._populate()

    # setup --------------------------------------------------------------------
    def _apply_bundle_icon(self):
        icon_path = bundle_file(ICON_FILE)
        if not os.path.isfile(icon_path):
            return
        try:
            self.set_icon(icon_path)
        except Exception:
            logger.debug('Window icon not applied:\n%s', traceback.format_exc())

    def _populate(self):
        """(Re)read all links from the model into the list."""
        records = read_link_records(self._doc)
        self._link_items.Clear()
        for record in records:
            self._link_items.Add(LinkItem(record, self._update_selection_summary))
        self._update_subtitle(records)
        self._update_selection_summary()
        self._update_search_state()

    def _repopulate(self, ids_to_check):
        """Refresh statuses after a run, keeping results and re-ticking given links."""
        previous_results = {}
        for item in self._link_items:
            state = item.get_result_state()
            if state is not None:
                previous_results[item.unique_id] = state
        self._populate()
        for item in self._link_items:
            state = previous_results.get(item.unique_id)
            if state is not None:
                item.set_result(*state)
            if item.unique_id in ids_to_check:
                item.is_checked = True

    def _update_subtitle(self, records):
        nested_count = sum(1 for record in records if record['nested'])
        top_level_count = len(records) - nested_count
        try:
            version = to_text(self._doc.Application.VersionNumber)
        except Exception:
            version = '?'
        text = '{0} in {1} (Revit {2}).'.format(
            plural(top_level_count, 'Revit link'), to_text(self._doc.Title), version)
        if nested_count:
            text += (' Nested links ({0}) are listed for reference; they reload with '
                     'their parent.'.format(nested_count))
        self.subtitle_text.Text = text

    # state --------------------------------------------------------------------
    def _checked_items(self):
        return [item for item in self._link_items if item.is_checked]

    def _matches_search(self, item):
        """Filter predicate: every search word must appear in the link name."""
        try:
            if not self._search_tokens:
                return True
            name = to_text(item.name).lower()
            for token in self._search_tokens:
                if token not in name:
                    return False
            return True
        except Exception:
            return True

    def _update_search_state(self):
        """Refresh the search hint, count, empty message and selection text."""
        has_text = bool(self.search_box.Text)
        self.search_hint.Visibility = Visibility.Collapsed if has_text else Visibility.Visible
        self.search_clear_button.Visibility = Visibility.Visible if has_text else Visibility.Collapsed
        if self._search_tokens:
            total = len(self._link_items)
            shown = sum(1 for item in self._link_items if self._matches_search(item))
            self.match_text.Text = '{0} of {1} shown'.format(shown, total)
            if shown == 0 and total > 0:
                self.empty_text.Text = u'No links match \u201c{0}\u201d.'.format(
                    self.search_box.Text.strip())
                self.empty_text.Visibility = Visibility.Visible
            else:
                self.empty_text.Visibility = Visibility.Collapsed
        else:
            self.match_text.Text = ''
            self.empty_text.Visibility = Visibility.Collapsed
        self._update_selection_text()

    def _update_selection_text(self):
        selectable = sum(1 for item in self._link_items if item.is_selectable)
        checked_items = self._checked_items()
        text = '{0} of {1} selected'.format(len(checked_items), plural(selectable, 'link'))
        if self._search_tokens:
            hidden = sum(1 for item in checked_items if not self._matches_search(item))
            if hidden:
                text += ' ({0} hidden by search - still included)'.format(hidden)
        self.selection_text.Text = text

    def _update_selection_summary(self):
        checked = len(self._checked_items())
        self._update_selection_text()
        if not self._busy:
            self.reload_button.IsEnabled = checked > 0
            self._set_progress(0, checked)

    def _set_progress(self, done, total):
        self.progress_bar.Maximum = max(total, 1)
        self.progress_bar.Value = done
        self.progress_text.Text = '{0} / {1}'.format(done, total)

    def _set_status(self, text):
        self.status_text.Text = text

    def _set_busy(self, busy):
        self._busy = busy
        for item in self._link_items:
            item.set_locked(busy)
        self.select_all_button.IsEnabled = not busy
        self.clear_all_button.IsEnabled = not busy
        self.search_box.IsEnabled = not busy
        self.search_clear_button.IsEnabled = not busy
        self.reload_button.IsEnabled = (not busy) and len(self._checked_items()) > 0
        self.close_button.Content = 'Stop' if busy else 'Close'
        self.close_button.IsEnabled = True
        self.Cursor = Cursors.AppStarting if busy else None

    def _pump_ui(self):
        """Let WPF repaint between links (same technique pyRevit uses)."""
        try:
            self.Dispatcher.Invoke(Action(_noop), DispatcherPriority.Background)
        except Exception:
            logger.debug('UI refresh skipped:\n%s', traceback.format_exc())

    def _scroll_into_view(self, item):
        try:
            self.links_grid.ScrollIntoView(item)
        except Exception:
            pass

    def _show_message(self, text, image=None):
        try:
            MessageBox.Show(self, text, TOOL_TITLE, MessageBoxButton.OK,
                            image if image is not None else MessageBoxImage.Information)
        except Exception:
            logger.debug('Message box failed:\n%s', traceback.format_exc())

    def _request_stop(self):
        if self._stop_requested:
            return
        self._stop_requested = True
        self.close_button.IsEnabled = False
        self._set_status('Stopping after the current link finishes...')

    def _handle_unexpected_error(self, error):
        logger.error('Unexpected error in %s:\n%s', TOOL_TITLE, traceback.format_exc())
        try:
            self._set_busy(False)
        except Exception:
            pass
        self._show_message('Something went wrong in the dialog:\n\n{0}'.format(
            describe_exception(error)), MessageBoxImage.Error)

    # event handlers -----------------------------------------------------------
    @ui_event
    def _on_select_all(self, sender, args):
        # Only links currently shown by the search are ticked.
        for item in self._link_items:
            if item.is_selectable and self._matches_search(item):
                item.is_checked = True

    @ui_event
    def _on_loaded(self, sender, args):
        self.search_box.Focus()

    @ui_event
    def _on_search_changed(self, sender, args):
        text = self.search_box.Text or ''
        self._search_tokens = [token for token in text.strip().lower().split() if token]
        self._link_view.Refresh()
        self._update_search_state()

    @ui_event
    def _on_search_key(self, sender, args):
        if args.Key == Key.Escape and self.search_box.Text:
            self.search_box.Text = ''
            args.Handled = True

    @ui_event
    def _on_search_clear(self, sender, args):
        self.search_box.Text = ''
        self.search_box.Focus()

    @ui_event
    def _on_clear_all(self, sender, args):
        for item in self._link_items:
            item.is_checked = False

    @ui_event
    def _on_close(self, sender, args):
        if self._busy:
            self._request_stop()
        else:
            self.Close()

    @ui_event
    def _on_closing(self, sender, args):
        # Closing mid-batch (Close/Stop button, Esc or the title bar X) only
        # requests a stop; the window stays open until the current link is done.
        if self._busy:
            args.Cancel = True
            self._request_stop()

    @ui_event
    def _on_reload(self, sender, args):
        if self._busy:
            return
        targets = self._checked_items()
        if not targets:
            self._set_status('Tick at least one link, then click Reload Selected.')
            return
        blocker = get_document_blocker(self._doc)
        if blocker:
            self._show_message(blocker, MessageBoxImage.Warning)
            return
        self._run_batch(targets)

    # batch --------------------------------------------------------------------
    def _run_batch(self, targets):
        """Reload the given links one by one. Failures never stop the batch."""
        total = len(targets)
        outcomes = []
        started = time.time()
        self._stop_requested = False
        self._set_busy(True)
        try:
            for item in targets:
                item.set_result('Queued', BRUSH_MUTED)
            self._set_progress(0, total)
            for index, item in enumerate(targets):
                if self._stop_requested:
                    break
                self._set_status('Reloading {0} of {1}: {2}'.format(index + 1, total, item.name))
                item.set_result('Reloading...', BRUSH_ACTIVE)
                self._scroll_into_view(item)
                self._pump_ui()

                outcome = reload_link(self._doc, item.type_id, item.unique_id, item.name)
                outcomes.append(outcome)
                item.show_outcome(outcome)
                self._set_progress(index + 1, total)
                self._pump_ui()
        finally:
            self._set_busy(False)

        not_processed = targets[len(outcomes):]
        for item in not_processed:
            item.set_result('Not processed (stopped)', BRUSH_MUTED)

        summary = BatchSummary(outcomes, [item.name for item in not_processed],
                               time.time() - started)
        ids_to_check = set(outcome.unique_id for outcome in summary.failed)
        ids_to_check.update(item.unique_id for item in not_processed)

        # Re-read statuses from the model. Failed and unprocessed links stay
        # ticked so the user can retry them straight away.
        self._repopulate(ids_to_check)
        self._set_progress(len(outcomes), total)
        self._set_status(summary.status_line())
        self._show_message(summary.details(),
                           MessageBoxImage.Warning if summary.failed else MessageBoxImage.Information)


# -----------------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------------
def main():
    try:
        doc = revit.doc
    except Exception:
        doc = None

    blocker = get_document_blocker(doc)
    if blocker:
        forms.alert(blocker, title=TOOL_TITLE)
        return

    if count_link_types(doc) == 0:
        forms.alert('This model contains no Revit links.', title=TOOL_TITLE)
        return

    xaml_path = bundle_file(XAML_FILE)
    if not os.path.isfile(xaml_path):
        forms.alert('The dialog layout file is missing:\n{0}\n\nKeep {1} in the same folder '
                    'as script.py.'.format(xaml_path, XAML_FILE), title=TOOL_TITLE)
        return

    ReloadLinksWindow(xaml_path, doc).show_dialog()


try:
    main()
except Exception as unexpected_error:
    logger.error('%s stopped unexpectedly:\n%s', TOOL_TITLE, traceback.format_exc())
    try:
        forms.alert('{0} stopped unexpectedly.\n\n{1}'.format(
            TOOL_TITLE, describe_exception(unexpected_error)), title=TOOL_TITLE)
    except Exception:
        pass
