# -*- coding: utf-8 -*-
"""Turns load records into workbook sheets.

Pure Python (no Revit API) so it can be unit tested outside Revit.

A *record* is one load element on one circuit (or one element in direct mode):
    panel, classification, circuit, identifier, family_type, category,
    element_id, connected_kw, demand_kw, apparent_kva, load_source,
    demand_estimated
"""
import re
from collections import defaultdict

from elr_xlsx import Sheet

try:
    text_type = unicode  # noqa: F821
except NameError:
    text_type = str

MODE_CIRCUITED = 'circuited'
MODE_DIRECT = 'direct'


def natural_key(value):
    """Sort 'MDB-2' before 'MDB-10' and circuit '3,5,7' before '10,12,14'."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r'(\d+)', text_type(value or u''))]


def _r(value, digits=4):
    return round(value or 0.0, digits)


# --------------------------------------------------------------------------- #
# Column model
# --------------------------------------------------------------------------- #
def load_columns(opts):
    """[(record key, header)] for the selected load columns."""
    direct = opts['mode'] == MODE_DIRECT
    cols = []
    if opts.get('include_connected') and not direct:
        cols.append(('connected_kw', u'Connected Load (kW)'))
    if opts.get('include_demand'):
        cols.append(('demand_kw', u'Demand Load (kVA)' if direct else u'Demand Load (kW)'))
    if opts.get('include_apparent'):
        cols.append(('apparent_kva', u'{0} (kVA)'.format(opts.get('direct_label') or u'Apparent Load')
                     if direct else u'Apparent Load (kVA)'))
    return cols


def primary_load(opts):
    """Load used for the 'Load Data' sheet and percentage shares."""
    if opts['mode'] == MODE_DIRECT:
        return 'apparent_kva', u'{0} (kVA)'.format(opts.get('direct_label') or u'Apparent Load')
    return 'connected_kw', u'Connected Load (kW)'


def _aggregate(records, key_fields, load_keys):
    groups = {}
    for rec in records:
        key = tuple(rec.get(f) or u'' for f in key_fields)
        bucket = groups.get(key)
        if bucket is None:
            bucket = groups[key] = {'count': 0, 'loads': dict((k, 0.0) for k in load_keys)}
        bucket['count'] += 1
        for k in load_keys:
            bucket['loads'][k] += rec.get(k) or 0.0
    return groups


def _totals_row(label, n_key_cols, count, load_values, include_count):
    row = [label] + [None] * (n_key_cols - 1)
    if include_count:
        row.append(count)
    return row + [_r(v) for v in load_values]


# --------------------------------------------------------------------------- #
# Sheets
# --------------------------------------------------------------------------- #
FIELD_HEADERS = {
    'panel': u'Panel',
    'classification': u'Load Classification',
    'circuit': u'Circuit',
    'identifier': u'Identifier',
    'family_type': u'Family : Type',
    'category': u'Category',
    'element_id': u'Element Id',
    'load_source': u'Load Source',
}


def detail_sheets(records, opts):
    direct = opts['mode'] == MODE_DIRECT
    cols = load_columns(opts)
    load_keys = [k for k, _ in cols]
    include_count = bool(opts.get('include_count'))

    key_fields = ([] if direct else ['panel']) + ['classification']
    if opts.get('include_circuit') and not direct:
        key_fields.append('circuit')

    if opts.get('group_classification'):
        groups = _aggregate(records, key_fields, load_keys)
        rows = []
        for key in sorted(groups, key=lambda k: [natural_key(x) for x in k]):
            g = groups[key]
            row = list(key) + ([g['count']] if include_count else [])
            rows.append(row + [_r(g['loads'][k]) for k in load_keys])
        fields = key_fields
    else:
        fields = key_fields + ['identifier', 'family_type', 'category', 'element_id', 'load_source']
        rows = []
        for rec in sorted(records, key=lambda r: [natural_key(r.get(f)) for f in fields]):
            row = [rec.get(f) for f in fields] + ([1] if include_count else [])
            rows.append(row + [_r(rec.get(k)) for k in load_keys])

    headers = [FIELD_HEADERS[f] for f in fields] + ([u'Count'] if include_count else []) + [h for _, h in cols]
    n_keys = len(fields)
    count_idx = n_keys if include_count else None

    def make(name, sheet_rows):
        count = sum(r[count_idx] for r in sheet_rows) if count_idx is not None else 0
        sums = [sum(r[i] or 0.0 for r in sheet_rows) for i in range(len(headers) - len(cols), len(headers))]
        return Sheet(name, headers, sheet_rows, _totals_row(u'Total', n_keys, count, sums, include_count))

    if opts.get('group_panel') and not direct:
        by_panel = defaultdict(list)
        for row in rows:
            by_panel[row[0]].append(row)
        return [make(panel, by_panel[panel]) for panel in sorted(by_panel, key=natural_key)]
    return [make(u'Direct Loads' if direct else u'Loads', rows)]


def summary_sheet(records, opts):
    direct = opts['mode'] == MODE_DIRECT
    cols = load_columns(opts)
    load_keys = [k for k, _ in cols]
    fields = ([] if direct else ['panel']) + ['classification']
    groups = _aggregate(records, fields, load_keys)
    rows = []
    for key in sorted(groups, key=lambda k: [natural_key(x) for x in k]):
        g = groups[key]
        rows.append(list(key) + [g['count']] + [_r(g['loads'][k]) for k in load_keys])
    headers = [FIELD_HEADERS[f] for f in fields] + [u'Count'] + [h for _, h in cols]
    totals = _totals_row(u'Grand Total', len(fields), len(records),
                         [sum(r.get(k) or 0.0 for r in records) for k in load_keys], True)
    return Sheet(u'Summary', headers, rows, totals)


def classification_sheet(records, opts):
    cols = load_columns(opts)
    load_keys = [k for k, _ in cols]
    pkey, _ = primary_load(opts)
    groups = _aggregate(records, ['classification'], load_keys + ([pkey] if pkey not in load_keys else []))
    grand = sum(r.get(pkey) or 0.0 for r in records)
    rows = []
    for key in sorted(groups, key=lambda k: -groups[k]['loads'][pkey]):
        g = groups[key]
        share = (g['loads'][pkey] / grand * 100.0) if grand else 0.0
        rows.append([key[0], g['count']] + [_r(g['loads'][k]) for k in load_keys] + [_r(share, 2)])
    headers = [u'Load Classification', u'Count'] + [h for _, h in cols] + [u'Share (%)']
    totals = [u'Grand Total', len(records)] + [_r(sum(r.get(k) or 0.0 for r in records)) for k in load_keys] + [100.0 if grand else 0.0]
    return Sheet(u'Classification Totals', headers, rows, totals)


def load_data_sheet(records, opts):
    pkey, pheader = primary_load(opts)
    if opts.get('sum_by_identifier'):
        sums = defaultdict(float)
        for rec in records:
            sums[(rec.get('identifier') or u'', rec.get('classification') or u'')] += rec.get(pkey) or 0.0
        rows = [[k[0], k[1], _r(v)] for k, v in sorted(sums.items(), key=lambda kv: (natural_key(kv[0][0]), natural_key(kv[0][1])))]
    else:
        rows = [[r.get('identifier'), r.get('classification'), _r(r.get(pkey))]
                for r in sorted(records, key=lambda r: (natural_key(r.get('identifier')), natural_key(r.get('classification'))))]
    totals = [u'Total', None, _r(sum(r[2] for r in rows))]
    return Sheet(u'Load Data', [u'Identifier', u'Load Classification', pheader], rows, totals)


# Two separate tables:
#   Fixture Loads by Class  the exported load elements (fixtures ...) added up
#                           per panel and classification
#   Panel Loads by Class    Revit's own panel values per classification (what
#                           an Electrical Equipment schedule shows)
PANEL_MATRIX_NAME = u'Fixture Loads by Class'
PANEL_REVIT_NAME = u'Panel Loads by Class'
UNCLASSIFIED = u'(none)'


def classification_code(name, codes=None):
    """Short column heading for a load classification.

    1. the Abbreviation of the Revit load classification with that name
       (codes = {classification name: abbreviation})
    2. else the part before " - "   ("KI - KITCHEN EQUIPMENT" -> "KI")
    3. else the name itself."""
    name = (name or u'').strip()
    if not name:
        return UNCLASSIFIED
    if codes:
        code = (codes.get(name) or u'').strip()
        if code:
            return code
    head = name.split(u' - ', 1)[0].strip()
    return head if (head and head != name and len(head) <= 12) else name


def panel_matrix_load(opts):
    """(record key, unit text) of the load shown in the panel table: apparent
    kVA when it is exported, else the first load column that is."""
    cols = load_columns(opts)
    for key, header in cols:
        if key == 'apparent_kva':
            return key, u'kVA'
    key, header = cols[0]
    return key, u'kVA' if u'kVA' in header else u'kW'


def panel_matrix_sheet(records, opts):
    """One row per panel, one column per load classification, the panel's
    load of that classification in the cell (empty when it has none), a
    Total column and a totals row. opts['all_classes'] (every classification
    of the project) adds the columns that no exported panel uses."""
    codes = opts.get('class_codes') or {}
    key, unit = panel_matrix_load(opts)
    cells = defaultdict(float)             # (panel, code) -> load
    panels, columns = set(), set()
    for rec in records:
        panel = rec.get('panel') or u''
        code = classification_code(rec.get('classification'), codes)
        panels.add(panel)
        columns.add(code)
        cells[(panel, code)] += rec.get(key) or 0.0
    # every classification of the project gets its column, used or not
    for name in opts.get('all_classes') or []:
        columns.add(classification_code(name, codes))
    columns = sorted(columns, key=lambda c: (c == UNCLASSIFIED, natural_key(c)))
    rows = []
    for panel in sorted(panels, key=natural_key):
        values = [cells.get((panel, code), 0.0) for code in columns]
        rows.append([panel] + [_r(v, 2) if v else None for v in values] + [_r(sum(values), 2)])
    column_totals = [sum(cells.get((panel, code), 0.0) for panel in panels) for code in columns]
    totals = [u'Total'] + [_r(v, 2) if v else None for v in column_totals] + [_r(sum(column_totals), 2)]
    headers = [u'Panel Name'] + columns + [u'Total ({0})'.format(unit)]
    return Sheet(PANEL_MATRIX_NAME, headers, rows, totals)


def panel_revit_sheet(opts):
    """The 'Panel Loads by Class' sheet, or None when the panels of this model
    have no per-classification load parameters."""
    revit_rows = opts.get('panel_class_loads')
    if not revit_rows:
        return None
    return _panel_matrix_from_revit(revit_rows, opts.get('all_classes') or [],
                                    opts.get('class_codes') or {})


def _panel_matrix_from_revit(revit_rows, class_names, codes):
    """The panel table from Revit's own panel parameters ("<classification>
    Connected Apparent Power", Total Connected, Total Demand) - the same
    numbers as an Electrical Equipment schedule with those fields.

    No totals row: a main panel already contains the load of the panels it
    feeds, so adding the rows up would count those loads twice."""
    columns = []
    for name in sorted(class_names, key=natural_key):
        code = classification_code(name, codes)
        if code not in columns:
            columns.append(code)
    rows = []
    for panel, values, connected, demand in sorted(revit_rows, key=lambda r: natural_key(r[0])):
        by_code = defaultdict(float)
        for name, value in values.items():
            by_code[classification_code(name, codes)] += value
        rows.append([panel] + [_r(by_code[c], 2) if by_code.get(c) else None for c in columns]
                    + [None if connected is None else _r(connected, 2),
                       None if demand is None else _r(demand, 2)])
    headers = [u'Panel Name'] + columns + [u'Total Connected (kVA)', u'Total Demand (kVA)']
    return Sheet(PANEL_REVIT_NAME, headers, rows)


def panel_check_sheet(records, panel_totals):
    """Reconcile exported loads against Revit's own panel totals."""
    exported = defaultdict(lambda: [0, 0.0, set()])
    for rec in records:
        e = exported[rec['panel']]
        e[0] += 1
        e[1] += rec.get('apparent_kva') or 0.0
        e[2].add(rec.get('circuit'))
    rows = []
    for panel in sorted(set(exported) | set(panel_totals), key=natural_key):
        count, kva, circuits = exported.get(panel, [0, 0.0, set()])
        revit_connected, revit_demand = panel_totals.get(panel, (None, None))
        share = (kva / revit_connected * 100.0) if revit_connected else None
        rows.append([panel, len(circuits), count, _r(kva),
                     None if revit_connected is None else _r(revit_connected),
                     None if revit_demand is None else _r(revit_demand),
                     None if share is None else _r(share, 1)])
    headers = [u'Panel', u'Circuits Exported', u'Loads Exported', u'Exported Apparent (kVA)',
               u'Revit Total Connected (kVA)', u'Revit Total Est. Demand (kVA)', u'Exported / Revit Connected (%)']
    return Sheet(u'Panel Check', headers, rows)


DIAG_LABELS = [
    ('elements_in_scope', u'Load elements in scope'),
    ('records', u'Load records exported'),
    ('not_circuited', u'Elements not on any power circuit'),
    ('no_panel', u'Circuit assignments without a panel'),
    ('panel_not_selected', u'Circuit assignments on unselected panels'),
    ('phase_filtered', u'Skipped by phase filter'),
    ('unknown_poles', u'Skipped: number of poles unknown'),
    ('circuit_share', u'Loads allocated from the unassigned part of a circuit (approximate)'),
    ('no_own_load', u'Not exported: no load of their own (the load is on another element of the circuit)'),
    ('demand_estimated', u'Demand not available (connected load used)'),
    ('zero_load', u'Records with zero load'),
    ('errors', u'Elements that raised errors'),
]


def diagnostics_sheet(run_info, diagnostics, warnings):
    rows = [[k, v] for k, v in run_info]
    rows.append([None, None])
    rows += [[label, diagnostics.get(key, 0)] for key, label in DIAG_LABELS if key in diagnostics]
    if warnings:
        rows.append([None, None])
        rows += [[u'Warning', w] for w in warnings[:2000]]
        if len(warnings) > 2000:
            rows.append([u'Warning', u'... {0} more not shown'.format(len(warnings) - 2000)])
    return Sheet(u'Diagnostics', [u'Item', u'Value'], rows, autofilter=False)


def build_sheets(records, opts, panel_totals=None, run_info=None, diagnostics=None, warnings=None):
    sheets = []
    if opts['mode'] == MODE_CIRCUITED:
        if opts.get('include_panel_matrix') and records:
            sheets.append(panel_matrix_sheet(records, opts))
        if opts.get('include_panel_revit'):
            revit_sheet = panel_revit_sheet(opts)
            if revit_sheet is not None:
                sheets.append(revit_sheet)
    if opts.get('include_summary'):
        sheets.append(summary_sheet(records, opts))
        sheets.append(classification_sheet(records, opts))
    if opts.get('include_panel_check') and opts['mode'] == MODE_CIRCUITED and panel_totals is not None:
        sheets.append(panel_check_sheet(records, panel_totals))
    sheets.append(load_data_sheet(records, opts))
    sheets.extend(detail_sheets(records, opts))
    if opts.get('include_diagnostics'):
        sheets.append(diagnostics_sheet(run_info or [], diagnostics or {}, warnings or []))
    return sheets


def explain_empty(diagnostics, opts):
    """Human-readable reason why nothing was exported."""
    d = diagnostics
    lines = [u'No loads matched the current settings.', u'']
    lines.append(u'- {0} load element(s) in scope'.format(d.get('elements_in_scope', 0)))
    if not d.get('elements_in_scope'):
        lines.append(u'  -> Check the scope and the load categories that are ticked.')
        return u'\n'.join(lines)
    hints = [
        ('not_circuited', u'element(s) are not on any power circuit', u'Use Direct mode, or circuit them in Revit.'),
        ('no_panel', u'circuit(s) have no panel assigned', u'Assign a panel in Revit.'),
        ('panel_not_selected', u'assignment(s) are on panels you did not tick', u'Tick more panels.'),
        ('phase_filtered', u'item(s) were removed by the phase filter', u'Change the phase filter to "All".'),
        ('unknown_poles', u'item(s) have no readable number of poles', u'Set the phase filter to "All".'),
        ('no_own_load', u'element(s) have no load of their own (another element on the same circuit '
                        u'carries it)', u'Tick that element\'s category (e.g. Mechanical Equipment).'),
        ('errors', u'element(s) could not be read (Revit error)',
         u'Tick "Diagnostics" and export again - the Warnings list shows each error.'),
    ]
    for key, text, fix in hints:
        if d.get(key):
            lines.append(u'- {0} {1}  -> {2}'.format(d[key], text, fix))
    return u'\n'.join(lines)
