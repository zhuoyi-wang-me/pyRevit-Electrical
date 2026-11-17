# -*- coding: utf-8 -*-
"""Minimal, dependency-free XLSX writer.

Runs under pyRevit IronPython 2.7 / 3.4 (uses .NET ZipArchive) and CPython
(falls back to the stdlib ``zipfile``), so the report pipeline can be unit
tested outside Revit. No Excel / COM automation is required.
"""
import math
import os
import re
import shutil
import sys
import tempfile
import uuid

try:  # IronPython 2.7
    text_type = unicode  # noqa: F821
    integer_types = (int, long)  # noqa: F821
except NameError:  # IronPython 3 / CPython
    text_type = str
    integer_types = (int,)

# Cell style indexes (must match _STYLES below)
S_DEFAULT, S_HEADER, S_NUM, S_INT, S_TOTAL_TEXT, S_TOTAL_NUM, S_TOTAL_INT = range(7)

_CTRL_CHARS = re.compile(u'[\x00-\x08\x0B\x0C\x0E-\x1F]')
_BAD_SHEET_CHARS = re.compile(r'[\\/*?:\[\]]')


class Sheet(object):
    """A worksheet: header row, data rows and an optional bold totals row."""

    def __init__(self, name, headers, rows, totals=None, autofilter=True):
        self.name = name
        self.headers = list(headers)
        self.rows = [list(r) for r in rows]
        self.totals = list(totals) if totals else None
        self.autofilter = autofilter


# --------------------------------------------------------------------------- #
# Text helpers
# --------------------------------------------------------------------------- #
def _text(value):
    if value is None:
        return u''
    if not isinstance(value, text_type):
        value = text_type(value)
    return _CTRL_CHARS.sub(u'', value)


def _esc(value):
    return (_text(value).replace(u'&', u'&amp;')
            .replace(u'<', u'&lt;').replace(u'>', u'&gt;'))


def _esc_attr(value):
    return _esc(value).replace(u'"', u'&quot;')


def column_letter(index):
    """1-based column index -> Excel letters (1 -> A, 27 -> AA)."""
    result = u''
    while index:
        index, rem = divmod(index - 1, 26)
        result = text_type(chr(65 + rem)) + result
    return result


def safe_sheet_name(name):
    name = _BAD_SHEET_CHARS.sub(u'-', _text(name)).strip().strip(u"'")
    return (name or u'Sheet')[:31]


def unique_sheet_names(names):
    used, result = set(), []
    for raw in names:
        base = safe_sheet_name(raw)
        candidate, n = base, 2
        while candidate.lower() in used:
            suffix = u' ({0})'.format(n)
            candidate = base[:31 - len(suffix)] + suffix
            n += 1
        used.add(candidate.lower())
        result.append(candidate)
    return result


def _is_number(value):
    return isinstance(value, integer_types + (float,)) and not isinstance(value, bool)


# --------------------------------------------------------------------------- #
# XML parts
# --------------------------------------------------------------------------- #
def _cell(ref, value, total=False):
    if value is None or value == u'':
        return u''
    if isinstance(value, bool):
        return u'<c r="{0}" t="b"><v>{1}</v></c>'.format(ref, 1 if value else 0)
    if _is_number(value):
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return u''
        is_int = isinstance(value, integer_types)
        style = (S_TOTAL_INT if is_int else S_TOTAL_NUM) if total else (S_INT if is_int else S_NUM)
        number = repr(value) if isinstance(value, float) else text_type(value)
        return u'<c r="{0}" s="{1}"><v>{2}</v></c>'.format(ref, style, number)
    style = S_TOTAL_TEXT if total else S_DEFAULT
    return (u'<c r="{0}" s="{1}" t="inlineStr"><is><t xml:space="preserve">{2}</t></is></c>'
            .format(ref, style, _esc(value)))


def _widths(sheet):
    widths = []
    every = [sheet.headers] + sheet.rows + ([sheet.totals] if sheet.totals else [])
    for col in range(len(sheet.headers)):
        longest = 0
        for row in every:
            if col < len(row) and row[col] is not None:
                value = row[col]
                if isinstance(value, float):
                    value = u'{0:,.2f}'.format(value)
                longest = max(longest, len(_text(value)))
        widths.append(min(60, max(10, longest + 3)))
    return widths


def worksheet_xml(sheet):
    ncols = max(1, len(sheet.headers))
    last_data_row = 1 + len(sheet.rows)
    parts = [u'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
             u'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
             u'<sheetViews><sheetView workbookViewId="0">'
             u'<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
             u'</sheetView></sheetViews>',
             u'<sheetFormatPr defaultRowHeight="15"/><cols>']
    for i, width in enumerate(_widths(sheet), 1):
        parts.append(u'<col min="{0}" max="{0}" width="{1}" customWidth="1"/>'.format(i, width))
    parts.append(u'</cols><sheetData>')

    parts.append(u'<row r="1" ht="24" customHeight="1">')
    for c, value in enumerate(sheet.headers, 1):
        parts.append(u'<c r="{0}1" s="{1}" t="inlineStr"><is><t xml:space="preserve">{2}</t></is></c>'
                     .format(column_letter(c), S_HEADER, _esc(value)))
    parts.append(u'</row>')

    for r, row in enumerate(sheet.rows, 2):
        parts.append(u'<row r="{0}">'.format(r))
        for c, value in enumerate(row, 1):
            parts.append(_cell(u'{0}{1}'.format(column_letter(c), r), value))
        parts.append(u'</row>')

    if sheet.totals:
        r = last_data_row + 1
        parts.append(u'<row r="{0}">'.format(r))
        for c in range(1, ncols + 1):
            value = sheet.totals[c - 1] if c - 1 < len(sheet.totals) else None
            ref = u'{0}{1}'.format(column_letter(c), r)
            # Paint the whole totals band even where the cell is empty.
            parts.append(_cell(ref, value, total=True) or
                         u'<c r="{0}" s="{1}"/>'.format(ref, S_TOTAL_TEXT))
        parts.append(u'</row>')

    parts.append(u'</sheetData>')
    if sheet.autofilter and sheet.headers:
        parts.append(u'<autoFilter ref="A1:{0}{1}"/>'.format(column_letter(ncols), max(1, last_data_row)))
    parts.append(u'</worksheet>')
    return u''.join(parts)


_STYLES = (
    u'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    u'<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    u'<fonts count="3">'
    u'<font><sz val="11"/><name val="Calibri"/></font>'
    u'<font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Calibri"/></font>'
    u'<font><b/><sz val="11"/><name val="Calibri"/></font>'
    u'</fonts>'
    u'<fills count="4">'
    u'<fill><patternFill patternType="none"/></fill>'
    u'<fill><patternFill patternType="gray125"/></fill>'
    u'<fill><patternFill patternType="solid"><fgColor rgb="FF0F2A44"/><bgColor indexed="64"/></patternFill></fill>'
    u'<fill><patternFill patternType="solid"><fgColor rgb="FFE2E8F0"/><bgColor indexed="64"/></patternFill></fill>'
    u'</fills>'
    u'<borders count="2">'
    u'<border><left/><right/><top/><bottom/><diagonal/></border>'
    u'<border><left/><right/><top style="thin"><color rgb="FF64748B"/></top><bottom/><diagonal/></border>'
    u'</borders>'
    u'<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    u'<cellXfs count="7">'
    u'<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    u'<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1" applyAlignment="1">'
    u'<alignment vertical="center" wrapText="1"/></xf>'
    u'<xf numFmtId="4" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
    u'<xf numFmtId="3" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
    u'<xf numFmtId="0" fontId="2" fillId="3" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1"/>'
    u'<xf numFmtId="4" fontId="2" fillId="3" borderId="1" xfId="0" applyNumberFormat="1" applyFont="1" applyFill="1" applyBorder="1"/>'
    u'<xf numFmtId="3" fontId="2" fillId="3" borderId="1" xfId="0" applyNumberFormat="1" applyFont="1" applyFill="1" applyBorder="1"/>'
    u'</cellXfs>'
    u'<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
    u'</styleSheet>')


def build_parts(sheets):
    """Return [(zip entry name, unicode xml)] for a complete workbook."""
    if not sheets:
        sheets = [Sheet(u'Report', [u'No data'], [])]
    names = unique_sheet_names([s.name for s in sheets])
    content_types = [
        u'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        u'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
        u'<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
        u'<Default Extension="xml" ContentType="application/xml"/>',
        u'<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>',
        u'<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>']
    sheet_tags, rels, defined, parts = [], [], [], []
    for i, (sheet, name) in enumerate(zip(sheets, names), 1):
        content_types.append(
            u'<Override PartName="/xl/worksheets/sheet{0}.xml" '
            u'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'.format(i))
        sheet_tags.append(u'<sheet name="{0}" sheetId="{1}" r:id="rId{1}"/>'.format(_esc_attr(name), i))
        rels.append(u'<Relationship Id="rId{0}" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                    u'relationships/worksheet" Target="worksheets/sheet{0}.xml"/>'.format(i))
        if sheet.autofilter and sheet.headers:
            quoted = name.replace(u"'", u"''")
            defined.append(u'<definedName name="_xlnm._FilterDatabase" localSheetId="{0}" hidden="1">'
                           u"'{1}'!$A$1:${2}${3}</definedName>".format(
                               i - 1, _esc(quoted), column_letter(len(sheet.headers)), max(1, 1 + len(sheet.rows))))
        parts.append((u'xl/worksheets/sheet{0}.xml'.format(i), worksheet_xml(sheet)))
    content_types.append(u'</Types>')
    rels.append(u'<Relationship Id="rIdStyles" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                u'relationships/styles" Target="styles.xml"/>')
    workbook = (u'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                u'<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                u'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                u'<sheets>{0}</sheets>{1}</workbook>').format(
                    u''.join(sheet_tags),
                    u'<definedNames>{0}</definedNames>'.format(u''.join(defined)) if defined else u'')
    return [
        (u'[Content_Types].xml', u''.join(content_types)),
        (u'_rels/.rels', u'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                         u'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                         u'<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                         u'relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>'),
        (u'xl/workbook.xml', workbook),
        (u'xl/_rels/workbook.xml.rels', u'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                                        u'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                                        + u''.join(rels) + u'</Relationships>'),
        (u'xl/styles.xml', _STYLES),
    ] + parts


# --------------------------------------------------------------------------- #
# Package writing
# --------------------------------------------------------------------------- #
def _zip_dotnet(path, parts):
    import clr
    clr.AddReference('System.IO.Compression')
    from System.IO import FileStream, FileMode, FileAccess, StreamWriter
    from System.IO.Compression import ZipArchive, ZipArchiveMode, CompressionLevel
    from System.Text import UTF8Encoding
    stream = FileStream(path, FileMode.Create, FileAccess.ReadWrite)
    archive = ZipArchive(stream, ZipArchiveMode.Create)
    try:
        for name, text in parts:
            # Explicit forward-slash entry names: CreateFromDirectory on older
            # .NET Framework builds wrote backslashes, which Excel rejects.
            entry = archive.CreateEntry(name, CompressionLevel.Optimal)
            writer = StreamWriter(entry.Open(), UTF8Encoding(False))
            try:
                writer.Write(text)
            finally:
                writer.Dispose()
    finally:
        archive.Dispose()
        stream.Dispose()


def _zip_stdlib(path, parts):
    import zipfile
    archive = zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED)
    try:
        for name, text in parts:
            archive.writestr(str(name), text.encode('utf-8'))
    finally:
        archive.close()


def _write_zip(path, parts):
    if sys.platform == 'cli':  # IronPython: .NET zip, works on .NET Framework 4.8 and .NET 8
        _zip_dotnet(path, parts)
    else:
        _zip_stdlib(path, parts)


def write_workbook(path, sheets):
    """Write the workbook and return the path actually used.

    The file is staged in %TEMP% (keeps OneDrive/antivirus off half-written
    files). If the target is locked (open in Excel) a numbered sibling is used.
    """
    staging = os.path.join(tempfile.gettempdir(), u'ELR_{0}.xlsx'.format(uuid.uuid4().hex))
    _write_zip(staging, build_parts(sheets))
    try:
        folder = os.path.dirname(path)
        if folder and not os.path.isdir(folder):
            os.makedirs(folder)
        root, ext = os.path.splitext(path)
        candidates = [path] + [u'{0} ({1}){2}'.format(root, n, ext or u'.xlsx') for n in range(2, 100)]
        last_error = None
        for candidate in candidates:
            if candidate != path and os.path.exists(candidate):
                continue
            try:
                shutil.copyfile(staging, candidate)
                return candidate
            except Exception as ex:  # IOError / .NET UnauthorizedAccessException
                last_error = ex
        raise IOError(u'Could not write workbook (is it open in Excel?): {0}'.format(last_error))
    finally:
        try:
            os.remove(staging)
        except Exception:
            pass
