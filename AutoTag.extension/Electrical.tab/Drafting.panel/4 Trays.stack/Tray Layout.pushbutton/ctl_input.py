# -*- coding: utf-8 -*-
"""Cable Tray Layout - reading and checking the cable schedule. No Revit here.

Reads .xlsx / .xlsm (directly, Excel does not have to be installed or closed)
and .csv, finds the columns by their headings and returns Tray objects.
"""
import csv
import os
import re

try:
    from StringIO import StringIO            # IronPython 2.7
except ImportError:                          # CPython 3
    from io import StringIO

from ctl_calc import Row, Tray

try:
    text_type = unicode
    PY2 = True
except NameError:
    text_type = str
    PY2 = False


class InputError(Exception):
    """The file cannot be used at all (wrong type, missing columns ...)."""


# key, heading shown to the user, must the column exist?
FIELDS = [
    ('tray_id', 'Tray ID (or Cable Tag)', True),
    ('fed_from', 'Fed from', False),
    ('fed_to', 'Fed to', False),
    ('cable_type', 'Cable type', True),
    ('main_od', 'Main cable outside diameter (mm)', True),
    ('ecc_od', 'ECC cable outside diameter (mm)', False),
    ('quantity', 'Quantity (number of runs)', False),
    ('tray_width', 'Tray width (mm)', True),
    ('tray_height', 'Tray height (mm)', True),
    ('layers', 'Number of layers', False),
]
TITLES = dict((key, title) for key, title, _ in FIELDS)

TEMPLATE = (
    u'Tray ID,Fed from,Fed to,Cable type,Main cable outside diameter (mm),ECC cable outside diameter (mm),Quantity,'
    u'Tray width (mm),Tray height (mm),Number of layers\r\n'
    u'CT-CC-B01-N-02-04,EMDB-CC-N-B1-01,MCC-CC-N-B1-04-01L,4C 240mm2 XLPE/SWA/PVC + 1C 120mm2 ECC,62.4,19.5,3,600,100,1\r\n'
    u'CT-CC-B01-N-02-04,EMDB-CC-N-B1-01,MCC-CC-N-03-03-01L,4C 95mm2 XLPE/SWA/PVC + 1C 50mm2 ECC,44.1,13.2,2,600,100,1\r\n'
    u'CT-CC-B01-N-02-05,EMDB-CC-N-B1-01,LIFT-CC-N-B1-04-01L,4C 16mm2 XLPE/SWA/PVC + 1C 16mm2 ECC,24.3,8.6,6,300,75,2\r\n'
)


# ---------------------------------------------------------------------------
# headings
# ---------------------------------------------------------------------------
def _field_of(heading):
    """Which field a column heading means, or None."""
    words = re.findall('[a-z0-9]+', text_type(heading or '').lower())
    if not words:
        return None
    has = lambda *names: any(name in words for name in names)
    starts = lambda prefix: any(word.startswith(prefix) for word in words)
    if has('from', 'source'):
        return 'fed_from'
    if has('to', 'destination'):
        return 'fed_to'
    # "OD", "O.D.", "Dia", "Diameter"
    diameter = has('od', 'dia') or (has('o') and has('d')) or starts('diam')
    earth = has('ecc', 'cpc', 'earth', 'earthing', 'pe')
    if diameter and earth:
        return 'ecc_od'
    if diameter:
        return 'main_od'
    if has('width') and (has('tray') or len(words) <= 3):
        return 'tray_width'
    if has('height', 'depth') and (has('tray') or len(words) <= 3):
        return 'tray_height'
    if starts('layer'):
        return 'layers'
    if has('qty', 'quantity', 'runs', 'nos') or (has('no', 'number') and has('cables', 'cable', 'runs', 'run')):
        return 'quantity'
    if has('tray', 'containment') and not has('type', 'size'):
        return 'tray_id'
    if has('tag'):
        return 'tag'                     # "CABLE TAG": the tray, when there is no tray column
    if has('cable', 'type', 'description') and not has('tray'):
        return 'cable_type'
    return None


def _find_header(table):
    """(row index, {field: column index}) of the heading row."""
    best = None
    for index, cells in enumerate(table[:30]):
        columns = {}
        for column, cell in enumerate(cells):
            field = _field_of(cell)
            if field and field not in columns:
                columns[field] = column
        tag = columns.pop('tag', None)
        if tag is not None and 'tray_id' not in columns:
            columns['tray_id'] = tag
        if 'tray_id' in columns and 'main_od' in columns:
            return index, columns
        if best is None or len(columns) > len(best[1]):
            best = (index, columns)
    return best if best is not None else (0, {})


# ---------------------------------------------------------------------------
# values
# ---------------------------------------------------------------------------
def number(value):
    """A cell as a float. None for an empty cell; ValueError for text that is
    not a number. '62,4', '62.4 mm' and '1,200.5' are understood."""
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = text_type(value).replace(u' ', ' ').strip()
    # "nothing here": an empty cell, one or more dashes ("-", "--", "---") or "n/a"
    if not text or not text.strip(u'-–—− ') or text.lower() in (u'n/a', u'na', u'n.a.', u'nil', u'none'):
        return None
    text = re.sub(r'(?i)\s*mm\.?$', '', text).replace(' ', '')
    if ',' in text and '.' in text:
        text = text.replace(',', '')                       # 1,200.5
    elif re.match(r'^[-+]?\d{1,3}(,\d{3})+$', text):
        text = text.replace(',', '')                       # 1,200
    else:
        text = text.replace(',', '.')                      # 62,4
    if not re.match(r'^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$', text):
        raise ValueError(text)
    return float(text)


# "N x cores C": the number of cables in front of the core count. Understood:
#   2x4C   2x3C   2 x 3C   2X3C   2*3C   2x3.5C   2x3,5C   2Rx3C   2 runs x 3C   (2x3Cx240)   LV 2x3C...
# It may stand anywhere in its half of the text (before the "+" for the main
# cable, after it for the earth cable); the first one found counts.
_RUNS = re.compile(u'(?<![\\d.,])(\\d+)\\s*(?:[rR](?:[uU][nN][sS]?)?\\s*)?[xX×*]\\s*\\d+(?:[.,]\\d+)?\\s*[cC]')


def runs_in_text(cable_text):
    """(number of main cables, number of earth cables or None) read from a
    cable description such as
        2x4Cx120mm2 CU/XLPE/SWA/LSZH + 1x1Cx120mm2 CU/LSZH (ECC)   -> (2, 1)
        1x4Cx185mm2 CU/XLPE/SWA/LSZH + 1x1Cx95mm2 CU/LSZH (ECC)    -> (1, 1)
        4C 16mm2 XLPE                                              -> (1, None)
    The first number of "N x cores C" is the number of cables. None for the
    earth cables means the text does not say: one per main cable."""
    text = text_type(cable_text or u'')
    # the main cable is described before the "+", the earth cable after it
    before, plus, after = text.partition(u'+')
    main = _RUNS.search(before)
    earth = _RUNS.search(after) if plus else None
    quantity = int(main.group(1)) if main else 1
    return (quantity if quantity >= 1 else 1), (int(earth.group(1)) if earth else None)


def _whole(value):
    """A cell as a whole number (None when empty)."""
    result = number(value)
    if result is None:
        return None
    if abs(result - round(result)) > 1e-9:
        raise ValueError(value)
    return int(round(result))


# ---------------------------------------------------------------------------
# files
# ---------------------------------------------------------------------------
def _net_stream(path):
    """The file opened for reading even while Excel has it open."""
    from System.IO import FileAccess, FileMode, FileShare, FileStream
    return FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite)


def _read_csv(path):
    from System.IO import StreamReader
    from System.Text import Encoding, UTF8Encoding
    stream = _net_stream(path)
    try:
        try:
            content = StreamReader(stream, UTF8Encoding(False, True)).ReadToEnd()
        except Exception:                                  # not UTF-8: a Windows "ANSI" file from Excel
            stream.Position = 0
            content = StreamReader(stream, Encoding.GetEncoding(1252)).ReadToEnd()
    finally:
        stream.Close()
    content = text_type(content).lstrip(u'﻿')
    first = content.split('\n', 1)[0]
    delimiter = max(',;\t', key=first.count)
    if PY2:
        reader = csv.reader(StringIO(content.encode('utf-8')), delimiter=delimiter)
        return [[cell.decode('utf-8').strip() for cell in line] for line in reader]
    return [[cell.strip() for cell in line] for line in csv.reader(StringIO(content), delimiter=delimiter)]


class _Workbook(object):
    """Just enough of the .xlsx format to read cell values."""

    def __init__(self, path):
        import clr
        for name in ('System.IO.Compression', 'System.IO.Compression.FileSystem', 'System.IO.Compression.ZipFile',
                     'System.Xml', 'System.Xml.ReaderWriter', 'System.Private.Xml'):
            try:
                clr.AddReference(name)
            except Exception:
                pass
        from System.IO.Compression import ZipArchive, ZipArchiveMode
        self._stream = _net_stream(path)
        try:
            self._zip = ZipArchive(self._stream, ZipArchiveMode.Read)
        except Exception:
            self._stream.Close()
            raise InputError('This is not a valid Excel workbook (.xlsx). If it is an old .xls file, save it as '
                             '.xlsx or as CSV first.')
        self.sheets = self._sheet_list()                   # [(name, part path)]

    def close(self):
        try:
            self._zip.Dispose()
        finally:
            self._stream.Close()

    def _xml(self, part):
        from System.IO import StreamReader
        from System.Xml import XmlDocument
        entry = self._zip.GetEntry(part)
        if entry is None:
            return None
        reader = StreamReader(entry.Open())
        try:
            document = XmlDocument()
            document.LoadXml(reader.ReadToEnd())
            return document.DocumentElement
        finally:
            reader.Close()

    @staticmethod
    def _children(node, name):
        return [child for child in node.ChildNodes if child.LocalName == name]

    @staticmethod
    def _attribute(node, name):
        for attribute in node.Attributes:
            if attribute.LocalName == name:
                return attribute.Value
        return None

    def _sheet_list(self):
        book = self._xml('xl/workbook.xml')
        links = self._xml('xl/_rels/workbook.xml.rels')
        if book is None or links is None:
            raise InputError('This workbook cannot be read. Save it again as .xlsx or as CSV.')
        targets = {}
        for link in self._children(links, 'Relationship'):
            target = self._attribute(link, 'Target') or ''
            target = target.lstrip('/') if target.startswith('/') else 'xl/' + target
            targets[self._attribute(link, 'Id')] = target
        found = []
        for holder in self._children(book, 'sheets'):
            for sheet in self._children(holder, 'sheet'):
                part = targets.get(self._attribute(sheet, 'id'))
                if part and '/worksheets/' in part:
                    found.append((text_type(self._attribute(sheet, 'name')), part))
        return found

    def _text_of(self, node):
        """All text of a string item, without the phonetic guide."""
        parts = []
        for child in node.ChildNodes:
            if child.LocalName == 't':
                parts.append(child.InnerText)
            elif child.LocalName == 'r':
                parts.extend(t.InnerText for t in self._children(child, 't'))
        return u''.join(text_type(part) for part in parts)

    def _shared(self):
        root = self._xml('xl/sharedStrings.xml')
        return [] if root is None else [self._text_of(item) for item in self._children(root, 'si')]

    def table(self, sheet_name):
        part = dict(self.sheets).get(sheet_name)
        if part is None:
            raise InputError('The workbook has no sheet named "{0}".'.format(sheet_name))
        root = self._xml(part)
        shared = self._shared()
        rows = {}
        for data in self._children(root, 'sheetData'):
            for row_number, row in enumerate(self._children(data, 'row'), 1):
                row_index = int(self._attribute(row, 'r') or row_number) - 1
                cells = rows.setdefault(row_index, {})
                for position, cell in enumerate(self._children(row, 'c')):
                    reference = self._attribute(cell, 'r') or ''
                    letters = ''.join(ch for ch in reference if ch.isalpha()).upper()
                    column = position
                    if letters:
                        column = 0
                        for ch in letters:
                            column = column * 26 + (ord(ch) - 64)
                        column -= 1
                    kind = self._attribute(cell, 't')
                    values = self._children(cell, 'v')
                    value = text_type(values[0].InnerText) if values else u''
                    if kind == 's' and value:
                        value = shared[int(value)]
                    elif kind == 'inlineStr':
                        inline = self._children(cell, 'is')
                        value = self._text_of(inline[0]) if inline else u''
                    cells[column] = value.strip()
        if not rows:
            return []
        width = max((max(cells) + 1) if cells else 0 for cells in rows.values())
        return [[rows.get(index, {}).get(column, u'') for column in range(width)] for index in range(max(rows) + 1)]


def is_excel(path):
    return os.path.splitext(path)[1].lower() in ('.xlsx', '.xlsm')


def sheet_names(path):
    """Sheet names of a workbook; [] for a CSV file."""
    if not is_excel(path):
        return []
    book = _Workbook(path)
    try:
        return [name for name, _ in book.sheets]
    finally:
        book.close()


def read_table(path, sheet=None):
    """The file as a list of rows, every cell a text."""
    if not os.path.isfile(path):
        raise InputError('The file does not exist:\n{0}'.format(path))
    extension = os.path.splitext(path)[1].lower()
    if extension == '.xls':
        raise InputError('Old .xls workbooks are not supported. Save the file as .xlsx or as CSV.')
    if extension in ('.xlsx', '.xlsm'):
        book = _Workbook(path)
        try:
            if not book.sheets:
                raise InputError('The workbook has no worksheets.')
            return book.table(sheet or book.sheets[0][0])
        finally:
            book.close()
    if extension in ('.csv', '.txt'):
        return _read_csv(path)
    raise InputError('Choose an Excel workbook (.xlsx) or a CSV file.')


# ---------------------------------------------------------------------------
# the schedule
# ---------------------------------------------------------------------------
def _settle_sizes(tray):
    """Work out the physical tray(s) behind one Tray ID from the size cells.

    A Tray ID can stand for ONE tray or for SEVERAL trays side by side:

      * the width is written once, or the same on every row   -> one tray
      * the width is written on only some rows, more than once
        (e.g. 900 on the first row, 750 on the second)         -> each width
        is one tray; the cables are shared out in schedule order: tray 1 is
        filled, the rest goes to tray 2 ... (done in ctl_calc.split_tray)
      * the width is written on EVERY row and changes           -> the rows
        with one width belong to that tray

    Sets tray.sizes = [(width, height, layers)], tray.width/height/layers (of
    the first), and row.size_index for the last case (else None)."""
    given = tray._given
    del tray._given
    listed = [entry for entry in given if entry['width'] is not None]
    heights = [entry['height'] for entry in given if entry['height'] is not None]
    layer_counts = [entry['layers'] for entry in given if entry['layers'] is not None]
    for row in tray.rows:
        row.size_index = None
    if not listed:
        tray.errors.append('The tray width is missing.')
    if not heights:
        tray.errors.append('The tray height is missing.')
    if not listed or not heights:
        return

    def size_of(entry, previous):
        height = entry['height'] if entry['height'] is not None else (previous[1] if previous else heights[0])
        layers = entry['layers'] if entry['layers'] is not None else (previous[2] if previous else
                                                                      (layer_counts[0] if layer_counts else 1))
        return (entry['width'], height, layers)

    every_row = len(listed) == len(given) and len(given) > 1
    sizes = []
    if every_row:
        # a new tray starts where the width changes
        for entry in given:
            if not sizes or abs(sizes[-1][0] - entry['width']) > 1e-9:
                sizes.append(size_of(entry, sizes[-1] if sizes else None))
            entry['index'] = len(sizes) - 1
        if len(sizes) > 1:
            for row in tray.rows:
                row.size_index = row._given.get('index')
    else:
        for entry in listed:
            sizes.append(size_of(entry, sizes[-1] if sizes else None))
    for row in tray.rows:
        del row._given
    tray.sizes = sizes
    tray.width, tray.height, tray.layers = sizes[0]


def parse(table):
    """(trays in file order, notes) from the rows of the file.

    Raises InputError when a required column is missing. Problems of single
    rows do not stop the reading: they are stored in tray.errors, and a tray
    with errors is not drawn."""
    header_index, columns = _find_header(table)
    missing = [title for key, title, required in FIELDS if required and key not in columns]
    if missing:
        seen = [cell for cell in (table[header_index] if table else []) if cell]
        raise InputError('Required column(s) not found:\n   - {0}\n\nHeadings found in the file:\n   {1}'.format(
            '\n   - '.join(missing), ', '.join(seen) if seen else '(none)'))
    notes = []
    for key in ('ecc_od', 'quantity', 'layers'):
        if key not in columns:
            notes.append('No "{0}" column: {1}.'.format(TITLES[key], {
                'ecc_od': 'no ECC cables are drawn', 'quantity': 'every row counts as 1 run',
                'layers': 'every tray gets 1 layer'}[key]))

    def cell(cells, key):
        column = columns.get(key)
        return cells[column] if column is not None and column < len(cells) else u''

    trays, by_id = [], {}
    current = None                                         # the tray of the row above (merged cells)
    filled_down = False
    for index in range(header_index + 1, len(table)):
        cells = table[index]
        line = index + 1
        if not any(text_type(value).strip() for value in cells):
            continue
        tray_id = text_type(cell(cells, 'tray_id')).strip()
        if not tray_id:
            if current is None:
                notes.append('Line {0}: no Tray ID, the row is ignored.'.format(line))
                continue
            tray_id, filled_down = current.id, True
        tray = by_id.get(tray_id)
        if tray is None:
            tray = by_id[tray_id] = Tray(tray_id)
            tray._seen = {}
            tray._given = []                               # the tray size cells of every row of this tray
            trays.append(tray)
        current = tray

        # tray size on this row (may be empty); what the sizes mean is decided
        # after the whole tray has been read - see _settle_sizes()
        given = {'width': None, 'height': None, 'layers': None}
        for key, attribute, label in (('tray_width', 'width', 'tray width'), ('tray_height', 'height', 'tray height')):
            raw = cell(cells, key)
            try:
                value = number(raw)
            except ValueError:
                tray.errors.append('Line {0}: {1} "{2}" is not a number.'.format(line, label, raw))
                continue
            if value is None:
                continue
            if value <= 0:
                tray.errors.append('Line {0}: {1} must be more than 0 (found {2}).'.format(line, label, raw))
            else:
                given[attribute] = value
        raw = cell(cells, 'layers')
        try:
            layers = _whole(raw)
            if layers is not None:
                if layers < 1:
                    raise ValueError(raw)
                given['layers'] = layers
        except ValueError:
            tray.errors.append('Line {0}: number of layers "{1}" must be a whole number of 1 or more.'.format(line, raw))
        tray._given.append(given)

        # the cable of this row
        problems = []
        main_od = ecc_od = None
        raw = cell(cells, 'main_od')
        try:
            main_od = number(raw)
            if main_od is None:
                problems.append('the main cable diameter is missing')
            elif main_od <= 0:
                problems.append('the main cable diameter must be more than 0 (found {0})'.format(raw))
        except ValueError:
            problems.append('the main cable diameter "{0}" is not a number'.format(raw))
        raw = cell(cells, 'ecc_od')
        try:
            ecc_od = number(raw)
            if ecc_od is not None and ecc_od < 0:
                problems.append('the ECC diameter must not be negative (found {0})'.format(raw))
            if not ecc_od:
                ecc_od = None                              # empty or 0: this run has no ECC
        except ValueError:
            problems.append('the ECC diameter "{0}" is not a number'.format(raw))
        raw = cell(cells, 'quantity')
        cable_text = text_type(cell(cells, 'cable_type')).strip()
        # "2x4Cx120mm2 ... + 1x1Cx120mm2 (ECC)" = 2 main cables and 1 earth cable
        quantity, earth_quantity = runs_in_text(cable_text)
        from_text = quantity > 1 or earth_quantity is not None
        try:
            value = _whole(raw)
            if value is not None:
                if value < 1:
                    raise ValueError(raw)
                # a Quantity column, when filled in, is the number of runs; the text is not counted again
                quantity, from_text = value, False
        except ValueError:
            problems.append('the quantity "{0}" must be a whole number of 1 or more'.format(raw))
        if problems:
            tray.errors.append('Line {0}: {1}.'.format(line, '; '.join(problems)))
            continue
        row = Row(len(tray.rows) + 1, cable_text, main_od, ecc_od,
                  quantity, line, text_type(cell(cells, 'fed_from')).strip(),
                  text_type(cell(cells, 'fed_to')).strip())
        row.earth_quantity = earth_quantity                # None = one earth cable per main cable
        row.quantity_in_text = from_text                   # the cable text already says "2x...": do not repeat it
        row._given = given
        tray.rows.append(row)

    for tray in trays:
        del tray._seen
        _settle_sizes(tray)
        if not tray.rows and not tray.errors:
            tray.errors.append('The tray has no cables.')
    if filled_down:
        notes.append('Rows without a Tray ID were taken as belonging to the tray above them.')
    if not trays:
        raise InputError('No cable rows were found under the headings.')
    return trays, notes


def load(path, sheet=None):
    return parse(read_table(path, sheet))
