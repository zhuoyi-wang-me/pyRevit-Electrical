# -*- coding: utf-8 -*-
"""pyRevit: copy local/linked Mechanical Equipment host parameters to electrical fixtures.

Modeless window: stays open over the Revit view so you can crossing-select
(or window-select) elements in the model while the tool is open, then hit
Refresh.

WHY EVERYTHING LIVES INSIDE THE CLASS
-------------------------------------
When a pyRevit script finishes, the engine clears the script's module globals.
A modeless window outlives that moment, so any method that referenced a
module-level helper or import would raise NameError on the first click
("name 'x' is not defined") - the window looks alive but every button is dead.

So: every helper is a @classmethod reached through cls/self, and every module
that methods need (revit, forms, Transaction, ...) is captured as a class
attribute at class-creation time, while the globals still exist. Nothing below
__init__ performs a module-global lookup.

Runs on both the IronPython 2.7 and CPython 3.x pyRevit engines.
"""
import os
import traceback

from Autodesk.Revit.DB import (BuiltInCategory, FilteredElementCollector,
                               RevitLinkInstance, StorageType, Transaction,
                               TransactionStatus)
from Autodesk.Revit.UI import IExternalEventHandler, ExternalEvent

from pyrevit import revit, forms, script

__persistentengine__ = True

_output = script.get_output()
_xaml = os.path.join(os.path.dirname(__file__), 'HostParameterCopy.xaml')

try:                        # IronPython 2.7
    _text_type = unicode    # noqa: F821
except NameError:           # CPython 3.x
    _text_type = str


class ActionHandler(IExternalEventHandler):
    """Drains a FIFO queue of callables inside a valid Revit API context.

    A queue, not a single slot: with one slot, an action that never executes
    blocks every later click permanently.
    """

    def __init__(self):
        self.queue = []
        self.on_error = None

    def Execute(self, uiapp):
        while self.queue:
            action = self.queue.pop(0)
            try:
                action()
            except Exception as ex:
                if self.on_error:
                    self.on_error(ex)

    def GetName(self):
        return 'CopyHostParametersActionHandler'


class HostParameterWindow(forms.WPFWindow):

    # -- captured while module globals are still alive -------------------------
    REVIT = revit
    FORMS = forms
    OUTPUT = _output
    TRACEBACK = traceback
    TRANSACTION = Transaction
    TX_STATUS = TransactionStatus
    STORAGE = StorageType
    BIC = BuiltInCategory
    COLLECTOR = FilteredElementCollector
    LINK_INSTANCE = RevitLinkInstance
    XAML_PATH = _xaml
    TEXT = _text_type
    SEPARATOR = u'  \u2192  '
    ALL_TYPES_LABEL = u'All eligible electrical fixtures'

    # -- Revit helpers (classmethods: reached via cls, never via globals) ------

    @classmethod
    def eid_value(cls, element_id):
        """ElementId.IntegerValue is deprecated in 2024+ and gone in newer APIs."""
        if element_id is None:
            return -1
        val = getattr(element_id, 'Value', None)      # Revit 2024+
        if val is None:
            val = element_id.IntegerValue             # Revit 2023 and earlier
        return int(val)

    @classmethod
    def bic_value(cls, bic):
        try:
            return int(bic)
        except (TypeError, ValueError):
            return int(bic.value__)

    @classmethod
    def is_category(cls, element, bic):
        if element is None:
            return False
        try:
            cat = element.Category
            if cat is None:
                return False
            native = getattr(cat, 'BuiltInCategory', None)   # Revit 2023+
            if native is not None:
                return native == bic
            return cls.eid_value(cat.Id) == cls.bic_value(bic)
        except Exception:
            return False

    @classmethod
    def is_mech(cls, element):
        return cls.is_category(element, cls.BIC.OST_MechanicalEquipment)

    @classmethod
    def is_context_error(cls, ex):
        """True when Revit rejected the call for being outside a valid API context."""
        text = u'{0} {1}'.format(type(ex).__name__, ex).lower()
        for marker in ('api context', 'outside of api', 'modeless'):
            if marker in text:
                return True
        return False

    @classmethod
    def host_of(cls, doc, fixture):
        """Resolve the hosting element, following a link instance into its document."""
        try:
            direct_host = fixture.Host
            if direct_host is not None and not isinstance(direct_host, cls.LINK_INSTANCE):
                return direct_host
        except Exception:
            pass
        try:
            ref = fixture.HostFace
            if ref is None:
                return None
            parent = doc.GetElement(ref.ElementId)
            if isinstance(parent, cls.LINK_INSTANCE):
                linked_doc = parent.GetLinkDocument()
                if linked_doc is not None and cls.eid_value(ref.LinkedElementId) != -1:
                    return linked_doc.GetElement(ref.LinkedElementId)
                return None
            return parent
        except Exception:
            return None

    @classmethod
    def fixtures_in_scope(cls, doc, uidoc, scope):
        bic = cls.BIC.OST_ElectricalFixtures
        if scope == 'selection':
            candidates = [doc.GetElement(eid) for eid in uidoc.Selection.GetElementIds()]
            return [e for e in candidates if cls.is_category(e, bic)]
        if scope == 'view':
            return list(cls.COLLECTOR(doc, uidoc.ActiveView.Id)
                        .OfCategory(bic).WhereElementIsNotElementType())
        return list(cls.COLLECTOR(doc).OfCategory(bic).WhereElementIsNotElementType())

    @classmethod
    def pairs(cls, doc, uidoc, scope='model'):
        result = []
        for fixture in cls.fixtures_in_scope(doc, uidoc, scope):
            host = cls.host_of(doc, fixture)
            if cls.is_mech(host):
                result.append((fixture, host))
        return result

    @classmethod
    def param_names(cls, element, writable_only=False):
        found = set()
        for p in element.Parameters:
            try:
                if p.Definition is None:
                    continue
                if writable_only and p.IsReadOnly:
                    continue
                found.add(p.Definition.Name)
            except Exception:
                continue
        return found

    @classmethod
    def fixture_type_name(cls, fixture):
        try:
            return u'{0} : {1}'.format(fixture.Symbol.Family.Name, fixture.Symbol.Name)
        except Exception:
            try:
                return cls.TEXT(fixture.Name)
            except Exception:
                return u'<unnamed type>'

    @classmethod
    def raw_value(cls, p):
        """Raw storage value, or None when the parameter holds nothing."""
        st = p.StorageType
        if st == cls.STORAGE.String:
            return p.AsString()          # None when unset - do NOT coerce to ''
        if st == cls.STORAGE.Integer:
            return p.AsInteger()
        if st == cls.STORAGE.Double:
            return p.AsDouble()
        if st == cls.STORAGE.ElementId:
            return p.AsElementId()
        return None

    @classmethod
    def display_value(cls, p):
        if p is None:
            return u'<missing>'
        try:
            shown = p.AsValueString()     # keeps the project's display units
            if shown is not None:
                return shown
        except Exception:
            pass
        try:
            raw = cls.raw_value(p)
            return u'<empty>' if raw is None else cls.TEXT(raw)
        except Exception:
            return u'<no value>'

    @classmethod
    def text_value(cls, p):
        """Best textual form of a parameter, for writing into a text parameter.
        Returns None when the source holds nothing."""
        st = p.StorageType
        if st == cls.STORAGE.String:
            return p.AsString()
        try:
            # For a Type parameter this yields the type name; for numbers it
            # yields the value formatted in the project's display units.
            shown = p.AsValueString()
            if shown:
                return shown
        except Exception:
            pass
        if st == cls.STORAGE.ElementId:
            eid = p.AsElementId()
            if cls.eid_value(eid) == -1:
                return None
            try:
                # Resolve in the parameter owner's document, which for a linked
                # host is the link document, not the host project.
                target = p.Element.Document.GetElement(eid)
                if target is not None:
                    name = getattr(target, 'Name', None)
                    if name:
                        return cls.TEXT(name)
            except Exception:
                pass
            return None
        raw = cls.raw_value(p)
        return None if raw is None else cls.TEXT(raw)

    @classmethod
    def plan(cls, fixture, host, source, dest, coerce_text=True):
        """Work out what to write. Returns (value, skip_reason); exactly one is set."""
        if source is None:
            return None, u'host parameter not present'
        if dest is None:
            return None, u'fixture parameter not present'
        if dest.IsReadOnly:
            return None, u'fixture parameter is read-only'

        if source.StorageType != dest.StorageType:
            if coerce_text and dest.StorageType == cls.STORAGE.String:
                text = cls.text_value(source)
                if not text:
                    return None, u'host value is empty'
                return text, None
            return None, u'storage types differ ({0} vs {1})'.format(
                source.StorageType, dest.StorageType)

        raw = cls.raw_value(source)
        if raw is None:
            return None, u'host value is empty'
        if source.StorageType == cls.STORAGE.ElementId:
            try:
                same_doc = host.Document.Equals(fixture.Document)
            except Exception:
                same_doc = False
            if not same_doc:
                return None, u'element-id values are meaningless across a link'
        return raw, None

    # -- construction ---------------------------------------------------------

    def __init__(self, doc, items, handler, ext_event, scope='model'):
        self._ready = False
        forms.WPFWindow.__init__(self, self.XAML_PATH)

        self.handler = handler
        self.ext_event = ext_event
        self.handler.on_error = self.fail

        self.doc = doc
        self.scope = scope
        self.all_items = items
        self.items = items

        # Plain lists. WPF is refreshed by assigning a NEW list to ItemsSource
        # rather than mutating in place, which a non-observable collection
        # cannot notify the UI about.
        self.map_pairs = []          # [(source_name, dest_name)]
        self.preview_rows = []
        self.push_mappings()
        self.push_preview()

        self.add_mapping.IsEnabled = False
        self.preview.IsEnabled = False

        self.reset_lists()
        self.source_combo.SelectionChanged += self.changed
        self.destination_combo.SelectionChanged += self.changed
        self._ready = True

    # -- plumbing -------------------------------------------------------------

    def say(self, message):
        try:
            self.status.Text = message or u''
        except Exception:
            pass

    def fail(self, ex):
        """Never let a failure be silent."""
        self.say(u'Error: {0}'.format(ex))
        try:
            self.OUTPUT.print_md(u'**Copy Host Parameters - error**\n\n```\n{0}\n```'.format(
                self.TRACEBACK.format_exc()))
        except Exception:
            pass

    def guard(self, label, func):
        """Immediate status feedback, then run. If the status text does not change
        on click, the handler never fired at all."""
        self.say(label)
        try:
            func()
        except Exception as ex:
            self.fail(ex)

    def enqueue(self, action):
        self.handler.queue.append(action)
        self.ext_event.Raise()

    def run_read(self, action):
        """Read-only work: run inline, fall back to the ExternalEvent only if
        Revit objects to the calling context."""
        try:
            action()
        except Exception as ex:
            if self.is_context_error(ex):
                self.say(u'Deferring to Revit idle time...')
                self.enqueue(action)
            else:
                self.fail(ex)

    def run_write(self, action):
        """Transactions must always go through the ExternalEvent."""
        self.enqueue(action)

    def push_mappings(self):
        self.mapping_list.ItemsSource = [u'{0}{1}{2}'.format(s, self.SEPARATOR, d)
                                         for s, d in self.map_pairs]

    def push_preview(self):
        self.preview_list.ItemsSource = list(self.preview_rows)

    def current_doc_ok(self):
        live = self.REVIT.doc
        if live is None:
            self.FORMS.alert('No Revit document is open.', warn_icon=True)
            return False
        try:
            if not self.doc.Equals(live):
                self.FORMS.alert('The active document changed. Click Refresh to re-read '
                                 'fixtures from the current document before copying.',
                                 warn_icon=True)
                return False
        except Exception:
            return False
        return True

    # -- list management ------------------------------------------------------

    def reset_lists(self):
        types = sorted(set(self.fixture_type_name(f) for f, h in self.all_items))
        self.fixture_filter.ItemsSource = [self.ALL_TYPES_LABEL] + types
        self.fixture_filter.SelectedIndex = 0
        self.items = self.all_items
        self.clear_working_state()
        self.update_items()

    def clear_working_state(self):
        del self.map_pairs[:]
        del self.preview_rows[:]
        self.push_mappings()
        self.push_preview()
        self.source_combo.SelectedIndex = -1
        self.destination_combo.SelectedIndex = -1

    def update_items(self):
        self.fixture_count.Text = str(len(self.items))
        source_names, dest_names = set(), set()
        for fixture, host in self.items:
            source_names |= self.param_names(host)
            dest_names |= self.param_names(fixture, writable_only=True)
        self.source_combo.ItemsSource = sorted(source_names, key=lambda x: x.lower())
        self.destination_combo.ItemsSource = sorted(dest_names, key=lambda x: x.lower())
        self.copy_button.IsEnabled = bool(self.items)

    # -- events ---------------------------------------------------------------

    def scope_changed(self, sender, args):
        if not self._ready or not getattr(sender, 'IsChecked', False):
            return
        if self.scope_selection.IsChecked:
            new_scope = 'selection'
        elif self.scope_view.IsChecked:
            new_scope = 'view'
        else:
            new_scope = 'model'
        if new_scope == self.scope:
            return
        self.scope = new_scope
        self.guard(u'Scope changed, reloading...',
                   lambda: self.run_read(self.do_refresh_scope))

    def refresh_click(self, sender, args):
        # Use after crossing/window-selecting elements in the model, or after
        # changing the active view, with this window open.
        self.guard(u'Refreshing...', lambda: self.run_read(self.do_refresh_scope))

    def do_refresh_scope(self):
        live = self.REVIT.doc
        if live is None:
            self.FORMS.alert('No Revit document is open.', warn_icon=True)
            return
        self.doc = live
        self.all_items = self.pairs(self.doc, self.REVIT.uidoc, self.scope)
        self.reset_lists()
        label = {'selection': 'the current selection',
                 'view': 'the active view'}.get(self.scope, 'the model')
        if not self.all_items:
            self.say(u'No eligible fixtures in {0}.'.format(label))
        else:
            self.say(u'Loaded {0} fixture/host pair(s) from {1}.'.format(
                len(self.all_items), label))

    def apply_filter_click(self, sender, args):
        self.guard(u'Applying filter...', lambda: self.run_read(self.do_apply_filter))

    def do_apply_filter(self):
        selected = self.fixture_filter.SelectedItem
        if selected is None or selected == self.ALL_TYPES_LABEL:
            self.items = self.all_items
        else:
            self.items = [(f, h) for f, h in self.all_items
                          if self.fixture_type_name(f) == selected]
        self.clear_working_state()
        self.update_items()
        self.say(u'Filter applied: {0} of {1} fixture(s) in play.'.format(
            len(self.items), len(self.all_items)))

    def changed(self, sender, args):
        enabled = bool(self.source_combo.SelectedItem and self.destination_combo.SelectedItem)
        self.add_mapping.IsEnabled = enabled
        self.preview.IsEnabled = enabled

    def coerce_enabled(self):
        """IsChecked is a nullable bool, so normalise it."""
        try:
            return bool(self.coerce_check.IsChecked)
        except Exception:
            return True

    def coerce_changed(self, sender, args):
        if not self._ready:
            return
        if self.source_combo.SelectedItem and self.destination_combo.SelectedItem:
            self.guard(u'Rebuilding preview...', lambda: self.run_read(self.do_preview))

    def preview_click(self, sender, args):
        self.guard(u'Building preview...', lambda: self.run_read(self.do_preview))

    def do_preview(self):
        source_name = self.source_combo.SelectedItem
        destination_name = self.destination_combo.SelectedItem
        del self.preview_rows[:]
        if not (source_name and destination_name):
            self.push_preview()
            self.say(u'Pick a host parameter and a fixture parameter first.')
            return
        shown = 0
        coerce_text = self.coerce_enabled()
        writable = 0
        for fixture, host in self.items:
            source = host.LookupParameter(source_name)
            dest = fixture.LookupParameter(destination_name)
            new_value, reason = self.plan(fixture, host, source, dest, coerce_text)
            if reason:
                result = u'skip: ' + reason
            else:
                writable += 1
                result = u'will write: {0}'.format(self.TEXT(new_value))
            self.preview_rows.append(u'{0}  |  {1}  |  {2}  |  {3}'.format(
                self.eid_value(fixture.Id),
                self.display_value(source),
                self.display_value(dest),
                result))
            shown += 1
            if shown >= 500:
                self.preview_rows.append(
                    u'... {0} more not shown'.format(len(self.items) - shown))
                break
        self.push_preview()
        self.say(u'{0} \u2192 {1}: {2} of {3} fixture(s) will be written.'.format(
            source_name, destination_name, writable, len(self.items)))

    def add_mapping_click(self, sender, args):
        self.guard(u'Adding mapping...', self.do_add_mapping)

    def do_add_mapping(self):
        source_name = self.source_combo.SelectedItem
        destination_name = self.destination_combo.SelectedItem
        if not (source_name and destination_name):
            self.say(u'Pick a host parameter and a fixture parameter first.')
            return
        pair = (source_name, destination_name)
        if pair in self.map_pairs:
            self.say(u'That mapping is already queued.')
            return
        self.map_pairs.append(pair)
        self.push_mappings()
        self.say(u'{0} mapping(s) queued.'.format(len(self.map_pairs)))

    def remove_mapping_click(self, sender, args):
        self.guard(u'Removing mapping...', self.do_remove_mapping)

    def do_remove_mapping(self):
        index = self.mapping_list.SelectedIndex
        if index < 0:
            self.say(u'Select a mapping in the list first.')
            return
        self.map_pairs.pop(index)
        self.push_mappings()
        self.say(u'{0} mapping(s) queued.'.format(len(self.map_pairs)))

    def copy_click(self, sender, args):
        self.guard(u'Queuing copy...', self.do_copy_request)

    def do_copy_request(self):
        if not self.map_pairs:
            self.say(u'Add at least one parameter mapping.')
            self.FORMS.alert('Add at least one parameter mapping.', warn_icon=True)
            return
        if not self.items:
            self.say(u'Nothing to copy in the current scope.')
            self.FORMS.alert('Nothing to copy in the current scope.', warn_icon=True)
            return
        if not self.current_doc_ok():
            return
        self.run_write(self.do_copy)

    def do_copy(self):
        copied = 0
        skips = {}

        def note(reason):
            skips[reason] = skips.get(reason, 0) + 1

        tx = self.TRANSACTION(
            self.doc, 'Copy Mechanical Host Parameters to Electrical Fixtures')
        tx.Start()
        coerce_text = self.coerce_enabled()
        try:
            # self.items, not self.all_items: honour the fixture-type filter.
            for fixture, host in self.items:
                for source_name, destination_name in self.map_pairs:
                    source = host.LookupParameter(source_name)
                    dest = fixture.LookupParameter(destination_name)
                    new_value, reason = self.plan(
                        fixture, host, source, dest, coerce_text)
                    if reason:
                        note(u'{0} \u2192 {1}: {2}'.format(
                            source_name, destination_name, reason))
                        continue
                    try:
                        ok = dest.Set(new_value)
                    except Exception as ex:
                        note(u'{0} \u2192 {1}: Revit rejected the value ({2})'.format(
                            source_name, destination_name, ex))
                        continue
                    if ok is False:
                        note(u'{0} \u2192 {1}: Revit rejected the value'.format(
                            source_name, destination_name))
                    else:
                        copied += 1
            tx.Commit()
        except Exception:
            if tx.GetStatus() == self.TX_STATUS.Started:
                tx.RollBack()
            raise

        skipped = sum(skips.values())
        report = [u'## Copy Host Parameters',
                  u'- Fixtures in scope: **{0}**'.format(len(self.items)),
                  u'- Values copied: **{0}**'.format(copied),
                  u'- Values skipped: **{0}**'.format(skipped)]
        if skips:
            report.append(u'\n**Skip reasons**\n')
            for reason, count in sorted(skips.items(), key=lambda kv: -kv[1]):
                report.append(u'- {0} \u00d7 {1}'.format(count, reason))
        self.OUTPUT.print_md(u'\n'.join(report))

        self.say(u'Copied {0}, skipped {1}. See the output window for details.'.format(
            copied, skipped))
        self.FORMS.alert('Completed: {0} copied; {1} skipped.\n\nSkip reasons are listed '
                         'in the pyRevit output window.'.format(copied, skipped))

    def cancel_click(self, sender, args):
        self.Close()


def main():
    doc = revit.doc
    if doc is None:
        forms.alert('Open a Revit project before running this tool.', warn_icon=True)
        return
    items = HostParameterWindow.pairs(doc, revit.uidoc, 'model')
    if not items:
        forms.alert('No electrical fixtures hosted by local or linked Mechanical '
                    'Equipment were found.', warn_icon=True)
        return
    handler = ActionHandler()
    ext_event = ExternalEvent.Create(handler)
    window = HostParameterWindow(doc, items, handler, ext_event, scope='model')
    window.say(u'Loaded {0} fixture/host pair(s) from the model.'.format(len(items)))
    # Modeless: stays open over the Revit view so you can keep selecting elements.
    window.Show()


main()
