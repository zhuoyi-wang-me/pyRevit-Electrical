# -*- coding: utf-8 -*-
"""Cable Tray QA/QC - reads the calculation list from Excel (no Revit API).

.xlsx / .xlsm files are read directly as Open XML (a zip of XML parts), so
Microsoft Excel does not have to be installed and no Excel process is started.
.csv is supported as well. Legacy .xls is not - save it as .xlsx.

read_calculation(path) returns an ExcelData; every problem a user can fix
(wrong file type, missing column, empty sheet) is raised as ExcelError with a
message that says what to do.
"""
import re

import clr
clr.AddReference('System.IO.Compression')
clr.AddReference('System.IO.Compression.FileSystem')
clr.AddReference('System.Xml')
from System.IO import File, FileAccess, FileMode, FileShare, FileStream, Path, StreamReader
from System.IO.Compression import ZipArchive, ZipArchiveMode
from System.Text import Encoding
from System.Xml import XmlDocument

import config
from annotation_parser import normalize_tag, to_number


class ExcelError(Exception):
    """A problem with the Excel file that the user can fix."""


class ExcelData(object):
    """The calculation list.

    rows        [{'tag', 'raw_tag', 'width', 'height', 'row', 'problems'}]
                one per Excel data row that has a tag (duplicates included)
    by_tag      {normalised tag: first row with that tag}
    duplicates  {normalised tag: [excel row numbers]} for tags listed twice
    """

    def __init__(self, path, sheet, header_row, headers):
        self.path = path
        self.sheet = sheet
        self.header_row = header_row
        self.headers = headers            # {'tag': 'Calculation_Tag', ...} as written
        self.rows = []
        self.by_tag = {}
        self.duplicates = {}
        self.skipped_blank = 0

    def tags(self):
        return set(self.by_tag)


# ---------------------------------------------------------------------------
# header matching
# ---------------------------------------------------------------------------
def _key(text):
    return re.sub(r'[\s_\-.()]+', '', u'{0}'.format(text or u'')).lower()


_TAG_KEYS = [_key(name) for name in config.TAG_HEADERS]
_WIDTH_KEYS = [_key(name) for name in config.WIDTH_HEADERS]
_HEIGHT_KEYS = [_key(name) for name in config.HEIGHT_HEADERS]


def _find_columns(row):
    """{'tag': index, 'width': index, 'height': index} for a header row, or
    None when the row does not hold all three. Earlier names in the config
    lists win, so 'Calculation_Tag' beats a generic 'Tag' column."""
    keys = [_key(cell) for cell in row]
    found = {}
    for field, wanted in (('tag', _TAG_KEYS), ('width', _WIDTH_KEYS), ('height', _HEIGHT_KEYS)):
        for name in wanted:
            if name in keys:
                found[field] = keys.index(name)
                break
    return found if len(found) == 3 else None


def _partial_columns(row):
    keys = [_key(cell) for cell in row]
    return [field for field, wanted in (('Calculation_Tag', _TAG_KEYS), ('Width', _WIDTH_KEYS),
                                        ('Height', _HEIGHT_KEYS))
            if [name for name in wanted if name in keys]]


# ---------------------------------------------------------------------------
# xlsx
# ---------------------------------------------------------------------------
def _xml(archive, entry_name):
    entry = archive.GetEntry(entry_name)
    if entry is None:
        return None
    reader = StreamReader(entry.Open())
    try:
        document = XmlDocument()
        document.LoadXml(reader.ReadToEnd())
        return document
    finally:
        reader.Close()


def _column_index(reference):
    index = 0
    for character in reference:
        if character.isalpha():
            index = index * 26 + ord(character.upper()) - 64
    return index - 1


def _shared_strings(archive):
    strings = []
    document = _xml(archive, 'xl/sharedStrings.xml')
    if document is None:
        return strings
    for item in document.GetElementsByTagName('si'):
        parts = []
        for child in item.ChildNodes:
            if child.LocalName == 't':
                parts.append(child.InnerText)
            elif child.LocalName == 'r':
                for run_child in child.ChildNodes:
                    if run_child.LocalName == 't':
                        parts.append(run_child.InnerText)
        strings.append(u''.join(parts))
    return strings


def _cell_text(cell, strings):
    kind = cell.GetAttribute('t')
    if kind == 'inlineStr':
        for child in cell.ChildNodes:
            if child.LocalName == 'is':
                return child.InnerText
        return u''
    raw = None
    for child in cell.ChildNodes:
        if child.LocalName == 'v':           # the VALUE, never the formula text
            raw = child.InnerText
            break
    if raw is None:
        return u''
    if kind == 's':
        try:
            return strings[int(raw)]
        except (ValueError, IndexError):
            return u''
    if kind == 'e':
        return u''
    return raw


def _sheet_rows(archive, entry_name, strings):
    """[(excel row number, [cell text, ...])] for the rows that hold anything."""
    document = _xml(archive, entry_name)
    rows = []
    if document is None:
        return rows
    position = 0
    for row_node in document.GetElementsByTagName('row'):
        position += 1
        try:
            number = int(row_node.GetAttribute('r'))
        except (ValueError, TypeError):
            number = position
        cells = {}
        for cell in row_node.GetElementsByTagName('c'):
            index = _column_index(cell.GetAttribute('r'))
            if index >= 0:
                cells[index] = _cell_text(cell, strings)
        if cells:
            rows.append((number, [cells.get(i, u'') for i in range(max(cells) + 1)]))
    return rows


def _read_xlsx(path):
    """[(sheet name, rows)] for the visible sheets, in workbook order."""
    try:
        stream = FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite)
    except Exception as error:
        raise ExcelError(u'The Excel file could not be opened:\n{0}'.format(error))
    try:
        try:
            archive = ZipArchive(stream, ZipArchiveMode.Read)
        except Exception:
            raise ExcelError('This is not a valid .xlsx workbook. If it is an old .xls file, '
                             'open it in Excel and save it as .xlsx.')
        try:
            workbook = _xml(archive, 'xl/workbook.xml')
            relations = _xml(archive, 'xl/_rels/workbook.xml.rels')
            if workbook is None or relations is None:
                raise ExcelError('This file is not a valid Excel workbook.')
            targets = {}
            for relation in relations.GetElementsByTagName('Relationship'):
                targets[relation.GetAttribute('Id')] = relation.GetAttribute('Target')
            namespace = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
            strings = _shared_strings(archive)
            sheets = []
            for sheet in workbook.GetElementsByTagName('sheet'):
                if sheet.GetAttribute('state') in ('hidden', 'veryHidden'):
                    continue
                target = targets.get(sheet.GetAttribute('id', namespace))
                if not target:
                    continue
                target = target.replace('\\', '/').lstrip('/')
                entry = target if target.startswith('xl/') else 'xl/' + target
                sheets.append((sheet.GetAttribute('name'), _sheet_rows(archive, entry, strings)))
            return sheets
        finally:
            archive.Dispose()
    finally:
        stream.Close()


# ---------------------------------------------------------------------------
# csv
# ---------------------------------------------------------------------------
def _split_csv_line(line, delimiter):
    cells, current, quoted, index = [], [], False, 0
    while index < len(line):
        character = line[index]
        if quoted:
            if character == '"':
                if index + 1 < len(line) and line[index + 1] == '"':
                    current.append('"')
                    index += 1
                else:
                    quoted = False
            else:
                current.append(character)
        elif character == '"':
            quoted = True
        elif character == delimiter:
            cells.append(u''.join(current))
            current = []
        else:
            current.append(character)
        index += 1
    cells.append(u''.join(current))
    return cells


def _read_csv(path):
    try:
        text = File.ReadAllText(path, Encoding.UTF8)
    except Exception as error:
        raise ExcelError(u'The CSV file could not be opened:\n{0}'.format(error))
    lines = text.replace('\r\n', '\n').replace('\r', '\n').split('\n')
    first = lines[0] if lines else ''
    delimiter = ';' if first.count(';') > first.count(',') else ','
    rows = [(number, _split_csv_line(line, delimiter))
            for number, line in enumerate(lines, 1) if line.strip()]
    return [(Path.GetFileNameWithoutExtension(path), rows)]


# ---------------------------------------------------------------------------
# public
# ---------------------------------------------------------------------------
def read_calculation(path):
    """Read the calculation list. Raises ExcelError with a clear message."""
    if not path or not File.Exists(path):
        raise ExcelError(u'The file was not found:\n{0}'.format(path))
    extension = (Path.GetExtension(path) or '').lower()
    if extension in ('.xlsx', '.xlsm'):
        sheets = _read_xlsx(path)
    elif extension == '.csv':
        sheets = _read_csv(path)
    elif extension == '.xls':
        raise ExcelError('Old .xls workbooks are not supported.\n\n'
                         'Open the file in Excel and save it as .xlsx, then select that file.')
    else:
        raise ExcelError(u'"{0}" files are not supported. Select an .xlsx, .xlsm or .csv file.'
                         .format(extension or '(no extension)'))
    if not sheets:
        raise ExcelError('The workbook has no visible worksheet.')

    if config.EXCEL_SHEET_NAME:
        wanted = config.EXCEL_SHEET_NAME.strip().lower()
        sheets = [item for item in sheets if item[0].strip().lower() == wanted]
        if not sheets:
            raise ExcelError(u'Worksheet "{0}" (set in config.py) is not in this workbook.'
                             .format(config.EXCEL_SHEET_NAME))

    # the first sheet with a row that holds all three headers
    chosen = None
    closest = []
    for name, rows in sheets:
        for position, (number, cells) in enumerate(rows[:config.HEADER_SCAN_ROWS]):
            columns = _find_columns(cells)
            if columns:
                chosen = (name, rows, position, number, columns, cells)
                break
            partial = _partial_columns(cells)
            if len(partial) > len(closest):
                closest = partial
        if chosen:
            break
    if chosen is None:
        missing = [name for name in ('Calculation_Tag', 'Width', 'Height') if name not in closest]
        raise ExcelError(
            u'The required columns were not found in the first {0} rows of any worksheet.\n\n'
            u'Missing: {1}\n\n'
            u'The file needs one header row with these three columns:\n'
            u'    Calculation_Tag    Width    Height\n\n'
            u'(Other accepted names are listed in config.py.)'
            .format(config.HEADER_SCAN_ROWS, ', '.join(missing)))

    name, rows, position, header_number, columns, header_cells = chosen
    data = ExcelData(path, name, header_number,
                     dict((field, header_cells[index]) for field, index in columns.items()))

    def cell(cells, index):
        return cells[index] if index < len(cells) else u''

    occurrences = {}
    for number, cells in rows[position + 1:]:
        raw_tag = cell(cells, columns['tag'])
        tag = normalize_tag(raw_tag)
        if not tag:
            if u''.join(cells).strip():
                data.skipped_blank += 1          # a row with data but no tag
            continue
        raw_width, raw_height = cell(cells, columns['width']), cell(cells, columns['height'])
        width, height = to_number(raw_width), to_number(raw_height)
        problems = []
        if width is None:
            problems.append(u'Excel Width is not a number ("{0}")'.format(raw_width.strip()))
        if height is None:
            problems.append(u'Excel Height is not a number ("{0}")'.format(raw_height.strip()))
        row = {'tag': tag, 'raw_tag': raw_tag, 'width': width, 'height': height,
               'row': number, 'problems': problems}
        data.rows.append(row)
        occurrences.setdefault(tag, []).append(number)
        data.by_tag.setdefault(tag, row)

    if not data.rows:
        raise ExcelError(u'Worksheet "{0}" has the three headers (row {1}) but no data rows '
                         u'with a Calculation_Tag below them.'.format(name, header_number))
    data.duplicates = dict((tag, numbers) for tag, numbers in occurrences.items()
                           if len(numbers) > 1)
    return data
