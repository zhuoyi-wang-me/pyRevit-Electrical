# -*- coding: utf-8 -*-
"""Equipment Power Check - does every mechanical equipment item have an electrical fixture?

READ-ONLY: no transaction, the model is never changed.

  1. Window: where the equipment is (this project / links), which categories,
     which fixtures count, how they are matched (tag parameter and/or distance).
  2. Every equipment item gets a status:
        OK - tag match       a fixture has the same tag value (e.g. MEE_ToServe)
        OK - fixture nearby  a fixture within the plan distance / height difference
        MISSING              neither
  3. Report in the pyRevit output window (MISSING first, clickable ids for
     elements of this project) + a CSV in Documents\\EquipmentPowerCheck.

    epc_match.py   matching (pure Python, unit-tested)
    elecplace      shared with Auto-Place Fixtures: links and equipment collection
"""
import datetime
import io
import os
import sys

from pyrevit import DB, forms, revit, script

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import epc_match as M                                                  # noqa: E402
from elecplace import planner                                          # noqa: E402
from elecplace.compat import (eid_int, element_name, ft_to_mm, mm_to_ft, symbol_label,  # noqa: E402
                              symbol_type_name, to_text)
from elecplace.linkdata import get_loaded_links                        # noqa: E402

TITLE = 'Equipment Power Check'
CONFIG = 'equipment_power_check'
doc = revit.doc
output = script.get_output()

TRACE_FILE = os.path.join(os.environ.get('APPDATA') or os.path.expanduser('~'),
                          'pyRevit', 'EquipmentPowerCheck_trace.log')


def trace(text):
    """Step trace flushed to disk: if Revit itself dies, the last line shows the step."""
    try:
        with io.open(TRACE_FILE, 'a', encoding='utf-8') as fh:
            fh.write(u'{0}  {1}\n'.format(datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), text))
            fh.flush()
            os.fsync(fh.fileno())
    except Exception:
        pass


FIXTURE_CATS = [('chkElecFix', 'OST_ElectricalFixtures'), ('chkElecEq', 'OST_ElectricalEquipment')]
EQUIP_BOXES = [('chkMech', 'mech'), ('chkPlumbEq', 'plumbeq'), ('chkVertCirc', 'vcirc')]
# window fields remembered between runs: (control name, default)
TEXT_FIELDS = [('txtEquipKeywords', u''), ('txtEquipExclude', u'plinth, chamber, thermostat'),
               ('txtFixKeywords', u''), ('txtIgnoreTags', u'GENERAL'),
               ('txtEquipParam', u'MEE_ToServe'), ('txtFixParam', u'MEE_ToServe'),
               ('txtMaxPlan', u'3000'), ('txtMaxHeight', u'4000')]
CHECK_FIELDS = [('chkOnlyConnector', False), ('chkFixLinks', False), ('chkTag', True),
                ('chkTypeFallback', True), ('chkDistance', True)]


# --------------------------------------------------------------------------- reading
def text_param(element, name):
    """Instance parameter, then type parameter, as text ('' when missing / empty)."""
    if not name or element is None:
        return u''
    owners = [element]
    try:
        owners.append(element.Document.GetElement(element.GetTypeId()))
    except Exception:
        pass
    for owner in owners:
        try:
            p = owner.LookupParameter(name)
        except Exception:
            p = None
        if p is None or not p.HasValue:
            continue
        try:
            value = p.AsString() if p.StorageType == DB.StorageType.String else p.AsValueString()
        except Exception:
            value = None
        if value and to_text(value).strip():
            return to_text(value).strip()
    return u''


def has_electrical_connector(inst):
    try:
        for c in inst.MEPModel.ConnectorManager.Connectors:
            if c.Domain == DB.Domain.DomainElectrical:
                return True
    except Exception:
        pass
    return False


def level_name(inst):
    try:
        lvl = inst.Document.GetElement(inst.LevelId)
        if lvl is not None:
            return element_name(lvl)
    except Exception:
        pass
    try:
        p = inst.get_Parameter(DB.BuiltInParameter.SCHEDULE_LEVEL_PARAM)
        if p is not None and p.HasValue:
            return element_name(inst.Document.GetElement(p.AsElementId()))
    except Exception:
        pass
    return u''


def point_of(inst):
    loc = inst.Location
    if isinstance(loc, DB.LocationPoint):
        return loc.Point
    try:
        bb = inst.get_BoundingBox(None)
        if bb is not None:
            return bb.Min.Add(bb.Max).Multiply(0.5)
    except Exception:
        pass
    return None


def collect_fixtures(links, cat_names, keywords, include_links, tag_param=u''):
    bics = [b for b in (planner.get_bic(n) for n in cat_names) if b is not None]
    if not bics:
        return []
    from System.Collections.Generic import List
    sources = [(u'this project', doc, None, 0)]
    if include_links:
        sources += [(lk.name, lk.doc, lk.transform, lk.id) for lk in links]
    items, seen = [], set()
    for src_name, sdoc, xf, link_id in sources:
        flt = DB.ElementMulticategoryFilter(List[DB.BuiltInCategory](bics))
        for inst in DB.FilteredElementCollector(sdoc).OfClass(DB.FamilyInstance).WherePasses(flt):
            if inst.SuperComponent is not None:
                continue
            label = symbol_label(inst.Symbol)
            if keywords and not any(k in label.lower() for k in keywords):
                continue
            p = point_of(inst)
            if p is None:
                continue
            if xf is not None:
                p = xf.OfPoint(p)
            key = (link_id, eid_int(inst.Id))
            if key in seen:
                continue
            seen.add(key)
            items.append(M.Item(key, u'{0} [{1}]'.format(label, eid_int(inst.Id)), src_name,
                                (p.X, p.Y, p.Z), tag=text_param(inst, tag_param),
                                level=level_name(inst),
                                element_id=inst.Id if link_id == 0 else None))
    return items


# --------------------------------------------------------------------------- window
class CheckWindow(forms.WPFWindow):
    def __init__(self, links):
        forms.WPFWindow.__init__(self, os.path.join(_HERE, 'ui.xaml'))
        self.links = links
        self.options = None
        self.sources = [(planner.SRC_ALL, u'This project + all linked models'),
                        (planner.SRC_HOST, u'This project only'),
                        (planner.SRC_LINKS, u'All linked models only')]
        for title in sorted(set(lk.title for lk in links)):
            self.sources.append((planner.SRC_LINK_PREFIX + title, u'Link: ' + title))
        for _key, text in self.sources:
            self.cbSource.Items.Add(text)
        self._restore()
        self.btnRun.Click += self.on_run
        self.btnClose.Click += lambda s, e: self.Close()

    def _restore(self):
        try:
            cfg = script.get_config(CONFIG)
            get = lambda k, d: cfg.get_option(k, d)
        except Exception:
            get = lambda k, d: d
        keys = [k for k, _t in self.sources]
        src = get('source', planner.SRC_ALL)
        self.cbSource.SelectedIndex = keys.index(src) if src in keys else 0
        for box, key in EQUIP_BOXES:
            getattr(self, box).IsChecked = str(get('eq_' + key, key == 'mech')).lower() in ('true', '1')
        for box, cat in FIXTURE_CATS:
            getattr(self, box).IsChecked = str(get('fx_' + cat, cat == 'OST_ElectricalFixtures')).lower() in ('true', '1')
        for name, default in TEXT_FIELDS:
            getattr(self, name).Text = to_text(get(name, default))
        for name, default in CHECK_FIELDS:
            getattr(self, name).IsChecked = str(get(name, default)).lower() in ('true', '1')

    def _save(self):
        try:
            cfg = script.get_config(CONFIG)
            o = self.options
            cfg.set_option('source', o['source'])
            for box, key in EQUIP_BOXES:
                cfg.set_option('eq_' + key, bool(getattr(self, box).IsChecked))
            for box, cat in FIXTURE_CATS:
                cfg.set_option('fx_' + cat, bool(getattr(self, box).IsChecked))
            for name, _default in TEXT_FIELDS:
                cfg.set_option(name, getattr(self, name).Text)
            for name, _default in CHECK_FIELDS:
                cfg.set_option(name, bool(getattr(self, name).IsChecked))
            script.save_config()
        except Exception:
            pass

    def on_run(self, sender, args):
        def words(box):
            return [w.strip().lower() for w in (box.Text or u'').split(u',') if w.strip()]

        def mm(box, label):
            try:
                value = float((box.Text or u'').strip().replace(u',', u'.'))
                if value <= 0:
                    raise ValueError()
                return value
            except ValueError:
                raise ValueError(u'"{0}" must be a positive number of millimetres.'.format(label))

        try:
            equip_cats = [key for box, key in EQUIP_BOXES if getattr(self, box).IsChecked]
            fix_cats = [cat for box, cat in FIXTURE_CATS if getattr(self, box).IsChecked]
            use_tag, use_dist = bool(self.chkTag.IsChecked), bool(self.chkDistance.IsChecked)
            if not equip_cats:
                raise ValueError(u'Tick at least one equipment category.')
            if not fix_cats:
                raise ValueError(u'Tick at least one fixture category.')
            if not (use_tag or use_dist):
                raise ValueError(u'Tick "by tag parameter", "by distance" or both.')
            if use_tag and not ((self.txtEquipParam.Text or u'').strip() and (self.txtFixParam.Text or u'').strip()):
                raise ValueError(u'Enter both tag parameter names, or untick "by tag parameter".')
            ignore_tags = words(self.txtIgnoreTags)
            if ignore_tags and not (self.txtFixParam.Text or u'').strip():
                raise ValueError(u'"Ignore fixtures tagged" needs the fixture parameter name '
                                 u'(under "How a fixture is matched").')
            self.options = {
                'source': self.sources[max(0, self.cbSource.SelectedIndex)][0],
                'equip_cats': equip_cats, 'equip_keywords': words(self.txtEquipKeywords),
                'equip_exclude': words(self.txtEquipExclude),
                'only_connector': bool(self.chkOnlyConnector.IsChecked),
                'fix_cats': fix_cats, 'fix_keywords': words(self.txtFixKeywords),
                'fix_links': bool(self.chkFixLinks.IsChecked), 'ignore_tags': ignore_tags,
                'use_tag': use_tag, 'equip_param': (self.txtEquipParam.Text or u'').strip(),
                'type_fallback': bool(self.chkTypeFallback.IsChecked),
                'fix_param': (self.txtFixParam.Text or u'').strip(),
                'use_distance': use_dist,
                'max_plan_mm': mm(self.txtMaxPlan, u'Max. plan distance') if use_dist else 3000.0,
                'max_height_mm': mm(self.txtMaxHeight, u'Max. height difference') if use_dist else 4000.0,
            }
        except ValueError as ex:
            forms.alert(to_text(ex), title=TITLE)
            return
        self._save()
        self.Close()


# --------------------------------------------------------------------------- report
# pyRevit's print_table / print_md run every table through its Markdown
# converter, which under IronPython recurses so deeply on a table of a few
# hundred rows that .NET runs out of stack and REVIT CRASHES (0xc00000fd).
# Tables are therefore written as plain HTML in chunks, with a row cap; the
# CSV always has every row.
MAX_ROWS = 300
CHUNK = 100


class Raw(type(u'')):
    """Text that is already HTML (a linkify() link) - not escaped."""


def _esc(value):
    if isinstance(value, Raw):
        return value
    return (to_text(value).replace(u'&', u'&amp;').replace(u'<', u'&lt;').replace(u'>', u'&gt;'))


def print_rows(columns, rows, limit=MAX_ROWS):
    head = u''.join(u'<th>{0}</th>'.format(_esc(c)) for c in columns)
    shown = rows[:limit]
    for start in range(0, len(shown), CHUNK):
        body = u''.join(u'<tr>{0}</tr>'.format(u''.join(u'<td>{0}</td>'.format(_esc(v)) for v in row))
                        for row in shown[start:start + CHUNK])
        output.print_html(u'<table><thead><tr>{0}</tr></thead><tbody>{1}</tbody></table>'.format(head, body))
    if len(rows) > limit:
        output.print_html(u'<p><i>... {0} more row(s) not shown here - every row is in the CSV.</i></p>'
                          .format(len(rows) - limit))


def heading(text):
    output.print_html(u'<h2>{0}</h2>'.format(_esc(text)))


def show(item):
    if item is None:
        return u''
    if item.element_id is not None:
        return Raw(output.linkify(item.element_id, item.label))
    return u'{0} ({1})'.format(item.label, item.source)


def write_csv(results, unused, o):
    folder = os.path.join(os.path.expanduser('~'), 'Documents', 'EquipmentPowerCheck')
    if not os.path.isdir(folder):
        os.makedirs(folder)
    safe_title = u''.join(c if c.isalnum() or c in u'-_' else u'_' for c in to_text(doc.Title))[:60]
    path = os.path.join(folder, u'{0}_{1}.csv'.format(
        safe_title, datetime.datetime.now().strftime('%Y%m%d-%H%M')))

    def cell(v):
        v = to_text(v)
        return u'"{0}"'.format(v.replace(u'"', u'""')) if any(c in v for c in u',"\n') else v

    # newline='' : the \r\n below is written as-is (text mode would make it \r\r\n,
    # which Excel shows as an empty line after every row)
    with io.open(path, 'w', encoding='utf-8-sig', newline=u'') as fh:
        fh.write(u'Status,Equipment,Model,Level,Equipment tag ({0}),Electrical connector,'
                 u'Tag-matched fixtures,Nearest fixture,Distance (mm),Note\r\n'.format(o['equip_param']))
        for r in results:
            e = r.equipment
            fh.write(u','.join(cell(v) for v in [
                r.status, e.label, e.source, e.level, tag_text(e),
                u'yes' if e.has_connector else u'no',
                u'; '.join(f.label for f in r.tag_fixtures),
                r.nearest.label if r.nearest else u'',
                int(round(ft_to_mm(r.distance))) if r.distance is not None else u'',
                r.note]) + u'\r\n')
        if unused:
            fh.write(u'\r\nFixtures whose tag matches no equipment\r\nFixture,Model,Level,Tag\r\n')
            for f in unused:
                fh.write(u','.join(cell(v) for v in [f.label, f.source, f.level, f.tag]) + u'\r\n')
    return path


def tag_text(item):
    """'FCU-14A (type name)' when the tag came from the type name."""
    if not item.tag:
        return u''
    return u'{0} ({1})'.format(item.tag, item.tag_from) if item.tag_from else item.tag


def report(results, unused, o, counts, csv_path):
    missing = [r for r in results if r.status == M.STATUS_MISSING]
    by_tag = sum(1 for r in results if r.status == M.STATUS_TAG)
    near = sum(1 for r in results if r.status == M.STATUS_NEAR)
    output.print_md(u'# {0}'.format(TITLE))
    output.print_md(
        u'**{0} equipment item(s) checked** ({1})  \n'
        u'**{2} MISSING** an electrical fixture  |  {3} OK by tag  |  {4} OK by a nearby fixture'.format(
            len(results), planner.source_description(o['source']), len(missing), by_tag, near))
    rules = []
    if o['use_tag']:
        rules.append(u"tag: equipment '{0}' = fixture '{1}'".format(o['equip_param'], o['fix_param']))
    if o['use_distance']:
        rules.append(u'nearby: within {0:.0f} mm in plan and {1:.0f} mm in height'.format(
            o['max_plan_mm'], o['max_height_mm']))
    output.print_md(u'Matched by ' + u'; '.join(rules) + u'.')
    notes = []
    if counts['excluded']:
        notes.append(u'{0} item(s) left out by name ({1})'.format(
            counts['excluded'], u', '.join(o['equip_exclude'])))
    if counts['no_connector']:
        notes.append(u'{0} item(s) without an electrical connector left out'.format(counts['no_connector']))
    if counts['from_type']:
        notes.append(u"{0} item(s) had no '{1}' value - their type name was used as tag".format(
            counts['from_type'], o['equip_param']))
    if counts['ignored']:
        notes.append(u'{0} fixture(s) ignored because they are tagged {1}'.format(
            counts['ignored'], u', '.join(t.upper() for t in o['ignore_tags'])))
    if notes:
        output.print_html(u'<ul>{0}</ul>'.format(u''.join(u'<li>{0}</li>'.format(_esc(n)) for n in notes)))

    columns = [u'Status', u'Equipment', u'Level', u'Tag', u'Nearest fixture', u'Distance (mm)', u'Note']

    def rows(items):
        return [[r.status, show(r.equipment), r.equipment.level, tag_text(r.equipment),
                 show(r.nearest) if r.nearest else (show(r.tag_fixtures[0]) if r.tag_fixtures else u''),
                 int(round(ft_to_mm(r.distance))) if r.distance is not None else u'', r.note]
                for r in items]

    if missing:
        heading(u'MISSING - no electrical fixture found ({0})'.format(len(missing)))
        print_rows(columns, rows(missing), limit=500)
    flagged = [r for r in results if r.status == M.STATUS_NEAR and r.note]
    if flagged:
        heading(u'OK by distance, but the tags do not agree ({0})'.format(len(flagged)))
        print_rows(columns, rows(flagged))
    n_ok = sum(1 for r in results if r.status != M.STATUS_MISSING) - len(flagged)
    if n_ok:
        heading(u'OK ({0})'.format(n_ok))
        output.print_html(u'<p>Not listed here to keep the window fast - every OK item is in the CSV.</p>')
    if unused:
        heading(u'Fixtures whose tag matches no equipment ({0})'.format(len(unused)))
        print_rows([u'Fixture', u'Level', u'Tag'], [[show(f), f.level, f.tag] for f in unused])
    output.print_html(u'<p>CSV saved: <code>{0}</code></p>'.format(_esc(csv_path)))


# --------------------------------------------------------------------------- main
def main():
    if doc.IsFamilyDocument:
        forms.alert('Open a project, not a family.', title=TITLE, exitscript=True)
    links, _unloaded = get_loaded_links(doc)
    window = CheckWindow(links)
    window.ShowDialog()
    o = window.options
    if not o:
        return
    trace(u'---- start: model "{0}", options {1}'.format(to_text(doc.Title), o))

    with forms.ProgressBar(title='Equipment Power Check - reading the models...', indeterminate=True) as bar:
        bar.update_progress(1, 2)
        found, _stats = planner.collect_equipment(doc, None, links, o['equip_cats'], o['source'],
                                                  o['equip_keywords'])
        equipment, no_connector, excluded, from_type = [], 0, 0, 0
        for eq in found:
            if M.name_excluded(symbol_label(eq.inst.Symbol), o['equip_exclude']):
                excluded += 1
                continue
            connector = has_electrical_connector(eq.inst)
            if o['only_connector'] and not connector:
                no_connector += 1
                continue
            tag, tag_from = u'', u''
            if o['use_tag']:
                tag, tag_from = M.tag_or_type(text_param(eq.inst, o['equip_param']),
                                              symbol_type_name(eq.inst.Symbol), o['type_fallback'])
                if tag_from:
                    from_type += 1
            p = eq.host_point
            equipment.append(M.Item(
                (eq.link_instance_id or 0, eq.elem_id), eq.label, eq.source, (p.X, p.Y, p.Z),
                tag=tag, tag_from=tag_from,
                level=level_name(eq.inst), has_connector=connector,
                element_id=eq.inst.Id if not eq.link_instance_id else None))
        read_tags = o['use_tag'] or bool(o['ignore_tags'])
        fixtures = collect_fixtures(links, o['fix_cats'], o['fix_keywords'], o['fix_links'],
                                    o['fix_param'] if read_tags else u'')
        fixtures, ignored = M.drop_ignored(fixtures, o['ignore_tags'])
        if not o['use_tag']:
            for f in fixtures:
                f.tag = u''              # tags were read only to drop the ignored ones
        bar.update_progress(2, 2)
    counts = {'excluded': excluded, 'no_connector': no_connector, 'from_type': from_type,
              'ignored': ignored}

    if not equipment:
        forms.alert(u'No equipment found in {0} for the ticked categories / names.'.format(
            planner.source_description(o['source'])), title=TITLE)
        return
    trace(u'read: {0} equipment ({1} excluded by name, {2} without connector left out, {3} tagged by '
          u'type name), {4} fixtures ({5} ignored by tag)'.format(
              len(equipment), excluded, no_connector, from_type, len(fixtures), ignored))
    results, unused = M.check(equipment, fixtures, o['use_tag'], o['use_distance'],
                              mm_to_ft(o['max_plan_mm']), mm_to_ft(o['max_height_mm']))
    trace(u'checked: {0} missing, {1} unused fixture tags'.format(
        sum(1 for r in results if r.status == M.STATUS_MISSING), len(unused)))
    csv_path = write_csv(results, unused, o)
    trace(u'csv written: ' + to_text(csv_path))
    trace(u'report: start')
    report(results, unused, o, counts, csv_path)
    trace(u'report: done - finished normally')


main()
