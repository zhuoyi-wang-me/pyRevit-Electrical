# -*- coding: utf-8 -*-
"""
Report tables and export.

CSV : one UTF-8 (BOM) file per sheet - opens correctly in Excel with Arabic /
      accented room names.
XLSX: native Office Open XML written with zipfile - no Excel, COM or openpyxl
      needed (openpyxl is not available under pyRevit IronPython).
"""
from __future__ import division

import codecs
import numbers
import os
import re
import zipfile


# ------------------------------------------------------------------- tables
def circuit_rows(plan):
    headers = ['Circuit ID', 'Status', 'Panel', 'Circuit No', 'Panel Type', 'Rule', 'Region',
               'Level', 'Rooms', 'Merged Rooms', 'Devices', 'Load (VA)', 'Poles', 'Dedicated', 'Message']
    rows = []
    for c in plan.circuits:
        rows.append([c.uid, c.status, c.panel.name if c.panel else '', c.circuit_number or '',
                     c.panel_type, c.rule_name, c.region, c.level_name, ', '.join(c.room_names),
                     ', '.join(c.merged_rooms), c.count, round(c.load_va, 1), c.poles,
                     'Yes' if c.dedicated else '', c.message])
    return headers, rows


def uncircuited(plan):
    """[(FixtureInfo, reason)] for everything that is not (going to be) circuited."""
    out = list(plan.skipped)
    ok = plan.success_status()
    for c in plan.circuits:
        if c.status != ok:
            for f in c.fixtures:
                out.append((f, '{0}: {1}'.format(c.status, c.message)))
    return out


def _dedupe(messages):
    counts, order = {}, []
    for m in messages:
        if m not in counts:
            order.append(m)
            counts[m] = 0
        counts[m] += 1
    return [(m, counts[m]) for m in order]


def build_sheets(plan):
    """Return [(sheet_name, headers, rows)]."""
    executed = plan.mode == 'execute'
    ok = plan.successful()
    ok_ids = set(f.element_id for c in ok for f in c.fixtures)
    missing = uncircuited(plan)

    summary = [
        ['Run ID', plan.run_id],
        ['Mode', 'Create circuits' if executed else 'Preview (no model changes)'],
        ['Started', plan.started],
        ['Duration (s)', round(plan.duration_s, 1)],
        ['Total fixtures/devices processed', plan.total_fixtures],
        ['Circuits ' + ('created' if executed else 'to be created'), len(ok)],
        ['Circuits not created', len(plan.circuits) - len(ok)],
        ['Devices circuited', len(ok_ids)],
        ['Uncircuited devices', len(missing)],
        ['Rooms merged', len(plan.merges)],
        ['Total connected load (VA)', round(sum(c.load_va for c in ok), 1)],
        ['Warnings', len(plan.warnings)],
        ['Errors', len(plan.errors)],
    ]
    for k in sorted(plan.info):
        summary.append([k, plan.info[k]])

    ch, crow = circuit_rows(plan)

    panel_rows = []
    for p in sorted(plan.panels, key=lambda x: x.name):
        pc = [c for c in ok if c.panel is p]
        new_poles = sum(c.poles for c in pc)
        util = ((p.used_poles + new_poles) / float(p.max_poles) * 100.0) if p.max_poles else 0.0
        panel_rows.append([p.name, p.panel_type or '', ', '.join(sorted(p.regions)), p.level_name,
                           p.max_poles, p.used_poles, new_poles, len(pc),
                           round(sum(c.load_va for c in pc), 1), round(util, 1),
                           'Locked (worksharing)' if p.locked else ''])

    unc_rows = [[f.element_id, f.category, f.name, f.classification, f.classification_source, f.room_name,
                 f.region or '', f.level_name, round(f.load_va, 1), reason] for f, reason in missing]

    merge_rows = [[m['into'], m['merged'], m['devices'], m['rule'], m['level'], m['region']]
                  for m in plan.merges]

    msg_rows = [['Error', m, n] for m, n in _dedupe(plan.errors)] + \
               [['Warning', m, n] for m, n in _dedupe(plan.warnings)]

    return [
        ('Summary', ['Metric', 'Value'], summary),
        ('Circuits', ch, crow),
        ('Panels', ['Panel', 'Type', 'Regions', 'Level', 'Max Poles', 'Existing Poles', 'New Poles',
                    'New Circuits', 'New Load (VA)', 'Utilization %', 'Note'], panel_rows),
        ('Uncircuited', ['Element Id', 'Category', 'Family : Type', 'Load Classification', 'Classification Source', 'Room',
                         'Region', 'Level', 'Load (VA)', 'Reason'], unc_rows),
        ('Room Merges', ['Merged Into', 'Merged Rooms', 'Devices', 'Rule', 'Level', 'Region'], merge_rows),
        ('Messages', ['Severity', 'Message', 'Occurrences'], msg_rows),
    ]


def default_basename(plan):
    return 'AutoCircuit_{0}_{1}'.format(plan.run_id or 'run', 'created' if plan.mode == 'execute' else 'preview')


# ---------------------------------------------------------------------- CSV
def _text(v):
    if v is None:
        return ''
    return v if isinstance(v, type(u'')) else u'{0}'.format(v)


def _csv_cell(v):
    s = _text(v)
    if any(ch in s for ch in (',', '"', '\n', '\r')):
        s = '"' + s.replace('"', '""') + '"'
    return s


def export_csv(sheets, folder, basename):
    if not os.path.isdir(folder):
        os.makedirs(folder)
    paths = []
    for name, headers, rows in sheets:
        path = os.path.join(folder, '{0}_{1}.csv'.format(basename, name.replace(' ', '')))
        with codecs.open(path, 'w', 'utf-8-sig') as fh:
            for row in [headers] + rows:
                fh.write(u','.join(_csv_cell(v) for v in row) + u'\r\n')
        paths.append(path)
    return paths


# --------------------------------------------------------------------- XLSX
_ILLEGAL_XML = re.compile(u'[\x00-\x08\x0b\x0c\x0e-\x1f]')


def _esc(v):
    s = _ILLEGAL_XML.sub(u'', _text(v))
    return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')


def _col(n):
    s = ''
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _cell(ref, v, style=0):
    st = ' s="{0}"'.format(style) if style else ''
    if isinstance(v, numbers.Number) and not isinstance(v, bool):
        return u'<c r="{0}"{1}><v>{2}</v></c>'.format(ref, st, repr(float(v)) if isinstance(v, float) else v)
    return u'<c r="{0}" t="inlineStr"{1}><is><t xml:space="preserve">{2}</t></is></c>'.format(ref, st, _esc(v))


def _sheet_xml(headers, rows):
    widths = [len(_text(h)) for h in headers]
    for row in rows:
        for i, v in enumerate(row):
            if i < len(widths):
                widths[i] = max(widths[i], min(len(_text(v)), 80))
    cols = u''.join(u'<col min="{0}" max="{0}" width="{1}" customWidth="1"/>'.format(i + 1, min(w + 2, 60))
                    for i, w in enumerate(widths))
    lines = []
    for r, row in enumerate([headers] + rows):
        cells = u''.join(_cell('{0}{1}'.format(_col(c), r + 1), v, 1 if r == 0 else 0)
                         for c, v in enumerate(row))
        lines.append(u'<row r="{0}">{1}</row>'.format(r + 1, cells))
    return (u'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            u'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            u'<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" '
            u'activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
            u'<cols>{0}</cols><sheetData>{1}</sheetData></worksheet>').format(cols, u''.join(lines))


_STYLES = (u'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           u'<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
           u'<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
           u'<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
           u'<fills count="3"><fill><patternFill patternType="none"/></fill>'
           u'<fill><patternFill patternType="gray125"/></fill>'
           u'<fill><patternFill patternType="solid"><fgColor rgb="FFDDE6F0"/></patternFill></fill></fills>'
           u'<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
           u'<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
           u'<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
           u'<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/></cellXfs>'
           u'<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
           u'</styleSheet>')


def export_xlsx(sheets, path):
    folder = os.path.dirname(path)
    if folder and not os.path.isdir(folder):
        os.makedirs(folder)
    names = []
    for name, _, _ in sheets:
        clean = re.sub(r'[\[\]\:\*\?\/\\]', '_', name)[:31]
        names.append(clean)

    ct = [u'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          u'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          u'<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          u'<Default Extension="xml" ContentType="application/xml"/>'
          u'<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
          u'<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>']
    ct += [u'<Override PartName="/xl/worksheets/sheet{0}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'.format(i + 1)
           for i in range(len(sheets))]
    ct.append(u'</Types>')

    rels = (u'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            u'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            u'<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            u'</Relationships>')

    wb = (u'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          u'<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
          u'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
          + u''.join(u'<sheet name="{0}" sheetId="{1}" r:id="rId{1}"/>'.format(_esc(n), i + 1)
                     for i, n in enumerate(names))
          + u'</sheets></workbook>')

    wb_rels = [u'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
               u'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">']
    wb_rels += [u'<Relationship Id="rId{0}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{0}.xml"/>'.format(i + 1)
                for i in range(len(sheets))]
    wb_rels.append(u'<Relationship Id="rId{0}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'.format(len(sheets) + 1))
    wb_rels.append(u'</Relationships>')

    try:
        zf = zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED)
    except (RuntimeError, ImportError):     # no zlib in some IronPython builds
        zf = zipfile.ZipFile(path, 'w', zipfile.ZIP_STORED)
    try:
        zf.writestr('[Content_Types].xml', u''.join(ct).encode('utf-8'))
        zf.writestr('_rels/.rels', rels.encode('utf-8'))
        zf.writestr('xl/workbook.xml', wb.encode('utf-8'))
        zf.writestr('xl/_rels/workbook.xml.rels', u''.join(wb_rels).encode('utf-8'))
        zf.writestr('xl/styles.xml', _STYLES.encode('utf-8'))
        for i, (_, headers, rows) in enumerate(sheets):
            zf.writestr('xl/worksheets/sheet{0}.xml'.format(i + 1), _sheet_xml(headers, rows).encode('utf-8'))
    finally:
        zf.close()
    return path
