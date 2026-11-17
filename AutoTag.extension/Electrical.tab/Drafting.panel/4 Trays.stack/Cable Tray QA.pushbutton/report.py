# -*- coding: utf-8 -*-
"""Cable Tray QA/QC - Excel report writer (no Revit API, no Excel needed).

Writes a real .xlsx (Open XML) with two worksheets:

    QA Report   one row per result, coloured by status, frozen header,
                filter buttons, sensible column widths
    Summary     the counts, the files compared and when

The workbook is built by hand as XML parts in a zip, so it works on machines
without Microsoft Excel and never starts an Excel process.
"""
import re
import time

import clr
clr.AddReference('System.IO.Compression')
clr.AddReference('System.IO.Compression.FileSystem')
from System.IO import File, FileMode, FileStream, StreamWriter
from System.IO.Compression import CompressionLevel, ZipArchive, ZipArchiveMode
from System.Text import UTF8Encoding

import config

COLUMNS = [
    # (header, row key, width, kind)
    ('Status', 'status', 26, 'status'),
    ('Calculation_Tag', 'tag', 30, 'text'),
    ('Sheet Number', 'sheet_number', 15, 'text'),
    ('Sheet Name', 'sheet_name', 34, 'text'),
    ('View Name', 'view_name', 34, 'text'),
    ('ElementId', 'element_id', 13, 'id'),
    ('Excel Width', 'excel_width', 13, 'number'),
    ('Excel Height', 'excel_height', 13, 'number'),
    ('Revit Width', 'revit_width', 13, 'number'),
    ('Revit Height', 'revit_height', 13, 'number'),
    ('Description', 'description', 40, 'text'),
    ('Comments', 'comments', 70, 'text'),
    ('Annotation ElementId', 'annotation_id', 20, 'id'),
    ('Annotation Type', 'family', 36, 'text'),
]

_ILLEGAL = re.compile(u'[\x00-\x08\x0b\x0c\x0e-\x1f]')
_STATUSES = list(config.STATUS_ORDER)

# cell style indexes (see _styles_xml)
S_DEFAULT, S_HEADER, S_TEXT, S_NUMBER, S_TITLE, S_LABEL = 0, 1, 2, 3, 4, 5
S_STATUS_BASE = 6


def _escape(value):
    text = _ILLEGAL.sub(u'', u'{0}'.format(value))
    return (text.replace(u'&', u'&amp;').replace(u'<', u'&lt;').replace(u'>', u'&gt;')
            .replace(u'"', u'&quot;'))


def _letter(index):
    letters = ''
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def _text_cell(reference, value, style):
    if value is None or value == '':
        return u'<c r="{0}" s="{1}"/>'.format(reference, style)
    return (u'<c r="{0}" s="{1}" t="inlineStr"><is><t xml:space="preserve">{2}</t></is></c>'
            .format(reference, style, _escape(value)))


def _number_cell(reference, value, style):
    if value is None:
        return _text_cell(reference, config.NOT_AVAILABLE, style)
    return u'<c r="{0}" s="{1}"><v>{2}</v></c>'.format(reference, style, '{0:g}'.format(float(value)))


def _status_style(status):
    return S_STATUS_BASE + (_STATUSES.index(status) if status in _STATUSES else 0)


def _styles_xml():
    fonts = [u'<font><sz val="10"/><name val="Segoe UI"/></font>',
             u'<font><b/><sz val="10"/><color rgb="FFFFFFFF"/><name val="Segoe UI"/></font>',
             u'<font><b/><sz val="16"/><color rgb="FF1F3A5F"/><name val="Segoe UI"/></font>',
             u'<font><b/><sz val="10"/><name val="Segoe UI"/></font>']
    fills = [u'<fill><patternFill patternType="none"/></fill>',
             u'<fill><patternFill patternType="gray125"/></fill>',
             u'<fill><patternFill patternType="solid"><fgColor rgb="FF1F3A5F"/></patternFill></fill>']
    for status in _STATUSES:
        fill, text = config.STATUS_COLOURS[status]
        fonts.append(u'<font><b/><sz val="10"/><color rgb="FF{0}"/><name val="Segoe UI"/></font>'
                     .format(text))
        fills.append(u'<fill><patternFill patternType="solid"><fgColor rgb="FF{0}"/></patternFill></fill>'
                     .format(fill))
    border = (u'<border><left style="thin"><color rgb="FFD0D7E2"/></left>'
              u'<right style="thin"><color rgb="FFD0D7E2"/></right>'
              u'<top style="thin"><color rgb="FFD0D7E2"/></top>'
              u'<bottom style="thin"><color rgb="FFD0D7E2"/></bottom><diagonal/></border>')
    wrap = u'<alignment vertical="top" wrapText="1"/>'
    xfs = [
        u'<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>',
        u'<xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" '
        u'applyBorder="1" applyAlignment="1"><alignment vertical="center" wrapText="1"/></xf>',
        u'<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1" '
        u'applyAlignment="1">' + wrap + u'</xf>',
        u'<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1" '
        u'applyAlignment="1"><alignment horizontal="right" vertical="top"/></xf>',
        u'<xf numFmtId="0" fontId="2" fillId="0" borderId="0" xfId="0" applyFont="1"/>',
        u'<xf numFmtId="0" fontId="3" fillId="0" borderId="0" xfId="0" applyFont="1"/>',
    ]
    for position in range(len(_STATUSES)):
        xfs.append(u'<xf numFmtId="0" fontId="{0}" fillId="{1}" borderId="1" xfId="0" applyFont="1" '
                   u'applyFill="1" applyBorder="1" applyAlignment="1">'
                   u'<alignment vertical="top"/></xf>'.format(4 + position, 3 + position))
    return (u'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            u'<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            u'<fonts count="{0}">{1}</fonts><fills count="{2}">{3}</fills>'
            u'<borders count="2"><border><left/><right/><top/><bottom/><diagonal/></border>{4}</borders>'
            u'<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
            u'<cellXfs count="{5}">{6}</cellXfs>'
            u'<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
            u'</styleSheet>').format(len(fonts), u''.join(fonts), len(fills), u''.join(fills),
                                     border, len(xfs), u''.join(xfs))


def _report_sheet_xml(rows):
    parts = [u'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
             u'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
             u'<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" '
             u'activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>',
             u'<cols>']
    for index, (_header, _key, width, _kind) in enumerate(COLUMNS, 1):
        parts.append(u'<col min="{0}" max="{0}" width="{1}" customWidth="1"/>'.format(index, width))
    parts.append(u'</cols><sheetData>')
    parts.append(u'<row r="1" ht="24" customHeight="1">')
    for index, (header, _key, _width, _kind) in enumerate(COLUMNS):
        parts.append(_text_cell(_letter(index) + '1', header, S_HEADER))
    parts.append(u'</row>')
    for number, row in enumerate(rows, 2):
        parts.append(u'<row r="{0}">'.format(number))
        for index, (_header, key, _width, kind) in enumerate(COLUMNS):
            reference = _letter(index) + str(number)
            value = row.get(key)
            if kind == 'status':
                parts.append(_text_cell(reference, value, _status_style(value)))
            elif kind == 'number':
                parts.append(_number_cell(reference, value, S_NUMBER))
            elif kind == 'id':
                parts.append(_text_cell(reference, config.NOT_AVAILABLE if value is None else value,
                                        S_NUMBER))
            else:
                parts.append(_text_cell(reference, value, S_TEXT))
        parts.append(u'</row>')
    parts.append(u'</sheetData>')
    parts.append(u'<autoFilter ref="A1:{0}{1}"/>'.format(_letter(len(COLUMNS) - 1), max(1, len(rows) + 1)))
    parts.append(u'</worksheet>')
    return u''.join(parts)


def _summary_sheet_xml(summary, meta):
    lines = [
        (S_TITLE, 'CABLE TRAY QA/QC', None, None),
        (None, None, None, None),
        (S_LABEL, 'Revit model', meta.get('model', ''), None),
        (S_LABEL, 'Excel calculation file', meta.get('excel', ''), None),
        (S_LABEL, 'Worksheet', meta.get('sheet', ''), None),
        (S_LABEL, 'Checked on', meta.get('date', ''), None),
        (S_LABEL, 'Annotations read', meta.get('readers', ''), None),
        (S_LABEL, 'Sheets checked', meta.get('sheets_checked', 'All sheets'),
         config.DUPLICATE if summary.get('partial') else None),
        (S_LABEL, 'Duplicate rule', config.DUPLICATE_SCOPE_LABELS.get(summary.get('duplicate_scope'), ''), None),
        (None, None, None, None),
        (S_LABEL, 'Excel Calculation Tags', summary['excel_tags'], None),
        (S_LABEL, 'Revit Annotation Tags', summary['revit_tags'], None),
        (S_LABEL, 'Annotations checked', summary['annotations'], None),
        (None, None, None, None),
        (S_LABEL, 'Matched', summary['matched'], config.PASS),
        (S_LABEL, 'Missing', summary['missing'], config.MISSING),
        (S_LABEL, 'Extra', summary['extra'], config.EXTRA),
        (S_LABEL, 'Width Mismatch', summary['width_mismatch'], config.WIDTH_MISMATCH),
        (S_LABEL, 'Height Mismatch', summary['height_mismatch'], config.HEIGHT_MISMATCH),
        (S_LABEL, 'Duplicates', summary['duplicates'], config.DUPLICATE),
        (S_LABEL, 'Invalid Annotations', summary['invalid'], config.INVALID),
        (S_LABEL, 'Cable Trays Not Tagged On A Sheet',
         summary.get('not_tagged', 0) if summary.get('coverage') else 'not checked', config.NOT_TAGGED),
        (S_LABEL, 'Overall Errors', summary['errors'],
         config.PASS if not summary['errors'] else config.MISSING),
    ]
    parts = [u'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
             u'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
             u'<cols><col min="1" max="1" width="30" customWidth="1"/>'
             u'<col min="2" max="2" width="70" customWidth="1"/></cols><sheetData>']
    for number, (style, label, value, status) in enumerate(lines, 1):
        parts.append(u'<row r="{0}">'.format(number))
        if label is not None:
            parts.append(_text_cell('A{0}'.format(number), label, style))
            if value is not None:
                value_style = _status_style(status) if status else S_DEFAULT
                if isinstance(value, (int, float)):
                    parts.append(u'<c r="B{0}" s="{1}"><v>{2}</v></c>'.format(number, value_style, value))
                else:
                    parts.append(_text_cell('B{0}'.format(number), value, value_style))
        parts.append(u'</row>')
    parts.append(u'</sheetData></worksheet>')
    return u''.join(parts)


def write_report(path, result, meta=None):
    """Write the QA report workbook. `result` is a comparison.QAResult.

    meta: {'model', 'excel', 'sheet', 'readers'} for the Summary sheet.
    Raises IOError with a clear message when the file cannot be written
    (usually because it is open in Excel)."""
    meta = dict(meta or {})
    meta.setdefault('date', time.strftime('%Y-%m-%d %H:%M'))
    sheets = [('QA Report', _report_sheet_xml(result.rows)),
              ('Summary', _summary_sheet_xml(result.summary, meta))]

    content_types = [u'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                     u'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                     u'<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                     u'<Default Extension="xml" ContentType="application/xml"/>'
                     u'<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                     u'<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>']
    workbook_sheets, relations = [], []
    for index, (name, _xml) in enumerate(sheets, 1):
        content_types.append(u'<Override PartName="/xl/worksheets/sheet{0}.xml" ContentType="application/'
                             u'vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'.format(index))
        workbook_sheets.append(u'<sheet name="{0}" sheetId="{1}" r:id="rId{1}"/>'.format(_escape(name), index))
        relations.append(u'<Relationship Id="rId{0}" Type="http://schemas.openxmlformats.org/officeDocument/'
                         u'2006/relationships/worksheet" Target="worksheets/sheet{0}.xml"/>'.format(index))
    relations.append(u'<Relationship Id="rId{0}" Type="http://schemas.openxmlformats.org/officeDocument/'
                     u'2006/relationships/styles" Target="styles.xml"/>'.format(len(sheets) + 1))
    content_types.append(u'</Types>')

    parts = [
        ('[Content_Types].xml', u''.join(content_types)),
        ('_rels/.rels', u'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                        u'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                        u'<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                        u'relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>'),
        ('xl/workbook.xml', u'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                            u'<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                            u'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                            u'<sheets>' + u''.join(workbook_sheets) + u'</sheets></workbook>'),
        ('xl/_rels/workbook.xml.rels', u'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                                       u'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                                       + u''.join(relations) + u'</Relationships>'),
        ('xl/styles.xml', _styles_xml()),
    ]
    for index, (_name, xml) in enumerate(sheets, 1):
        parts.append(('xl/worksheets/sheet{0}.xml'.format(index), xml))

    try:
        if File.Exists(path):
            File.Delete(path)
        stream = FileStream(path, FileMode.Create)
    except Exception as error:
        raise IOError(u'The report could not be written. If it is open in Excel, close it and '
                      u'export again.\n\n{0}'.format(error))
    try:
        archive = ZipArchive(stream, ZipArchiveMode.Create)
        try:
            encoding = UTF8Encoding(False)
            for name, xml in parts:
                writer = StreamWriter(archive.CreateEntry(name, CompressionLevel.Optimal).Open(), encoding)
                try:
                    writer.Write(xml)
                finally:
                    writer.Close()
        finally:
            archive.Dispose()
    finally:
        stream.Close()
    return path
