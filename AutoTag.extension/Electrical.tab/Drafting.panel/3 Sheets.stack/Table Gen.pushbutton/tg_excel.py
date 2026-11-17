# -*- coding: utf-8 -*-
"""Table Gen - reads one Excel worksheet WITH its look (no Revit API, Excel need not be installed).

An .xlsx / .xlsm file is a zip of XML parts; it is read directly. What is read:
    - the text of every cell as Excel SHOWS it (number formats: decimals, thousands, %, dates, text)
    - column widths and row heights, hidden rows / columns
    - merged cells
    - font name, size, bold, italic, underline, colour
    - horizontal / vertical alignment, wrap text, indent
    - cell borders (thin / medium / thick) and solid fill colours (incl. theme colours and tints)
    - the print area (used as the default range)
A .csv file gives a plain table (no formatting).

    wb = Workbook(path)          -> wb.sheets  [SheetInfo(name, print_area)]
    table = build_table(wb, sheet_name, range_text, skip_hidden=True, borders='excel')

All sizes in the Table are PAPER millimetres; x to the right, y DOWN as negative numbers
(the top-left corner of the table is 0,0).
"""
import re
import datetime
import colorsys

import clr
clr.AddReference('System.IO.Compression')
clr.AddReference('System.Xml')
from System.IO import File, FileAccess, FileMode, FileShare, FileStream, MemoryStream, Path, StreamReader
from System.IO.Compression import ZipArchive, ZipArchiveMode
from System.Text import Encoding
from System.Xml import XmlDocument, XmlNodeType

PX_MM = 25.4 / 96.0          # one screen pixel at 100 % zoom
PT_MM = 25.4 / 72.0          # one point
REL_NS = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'


class TableError(Exception):
    """A problem the user can fix (wrong file, bad range, empty sheet)."""


# ============================================================================ xml helpers
def _kids(node, name=None):
    if node is None:
        return []
    return [c for c in node.ChildNodes
            if c.NodeType == XmlNodeType.Element and (name is None or c.LocalName == name)]


def _kid(node, name):
    for child in _kids(node, name):
        return child
    return None


def _attr(node, name, default=None):
    if node is None:
        return default
    value = node.GetAttribute(name)
    return value if value else default


def _flag(node, name, default=False):
    """<b/>, <b val="1"/>, <b val="true"/> -> True; <b val="0"/> -> False."""
    child = _kid(node, name) if node is not None else None
    if child is None:
        return default
    value = (_attr(child, 'val', '1') or '1').lower()
    return value not in ('0', 'false')


def _num(text, default=None):
    if text is None or text == '':
        return default
    try:
        return float(text)
    except Exception:                           # IronPython: float(None) is a SystemError
        return default


# ============================================================================ colours
INDEXED = ['000000', 'FFFFFF', 'FF0000', '00FF00', '0000FF', 'FFFF00', 'FF00FF', '00FFFF',
           '000000', 'FFFFFF', 'FF0000', '00FF00', '0000FF', 'FFFF00', 'FF00FF', '00FFFF',
           '800000', '008000', '000080', '808000', '800080', '008080', 'C0C0C0', '808080',
           '9999FF', '993366', 'FFFFCC', 'CCFFFF', '660066', 'FF8080', '0066CC', 'CCCCFF',
           '000080', 'FF00FF', 'FFFF00', '00FFFF', '800080', '800000', '008080', '0000FF',
           '00CCFF', 'CCFFFF', 'CCFFCC', 'FFFF99', '99CCFF', 'FF99CC', 'CC99FF', 'FFCC99',
           '3366FF', '33CCCC', '99CC00', 'FFCC00', 'FF9900', 'FF6600', '666699', '969696',
           '003366', '339966', '003300', '333300', '993300', '993366', '333399', '333333']
# Office default theme, in Excel's theme-index order (lt1, dk1, lt2, dk2, accent1-6, hlink, folHlink)
DEFAULT_THEME = ['FFFFFF', '000000', 'E7E6E6', '44546A', '4472C4', 'ED7D31', 'A5A5A5', 'FFC000',
                 '5B9BD5', '70AD47', '0563C1', '954F72']


def _tint(rgb, tint):
    """Excel tint: darker (tint < 0) or lighter (tint > 0) in HLS space."""
    r, g, b = [c / 255.0 for c in rgb]
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    l = l * (1.0 + tint) if tint < 0 else l * (1.0 - tint) + tint
    r, g, b = colorsys.hls_to_rgb(h, max(0.0, min(1.0, l)), s)
    return (int(round(r * 255)), int(round(g * 255)), int(round(b * 255)))


def _hex(text):
    text = (text or '')[-6:]
    try:
        return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))
    except ValueError:
        return None


def _theme_colours(root):
    """Excel theme-index order from xl/theme/theme1.xml (dk1/lt1 and dk2/lt2 are swapped there)."""
    if root is None:
        return list(DEFAULT_THEME)
    scheme = None
    for node in root.GetElementsByTagName('*'):
        if node.LocalName == 'clrScheme':
            scheme = node
            break
    if scheme is None:
        return list(DEFAULT_THEME)
    found = {}
    for child in _kids(scheme):
        value = None
        for sub in _kids(child):
            value = _attr(sub, 'val') if sub.LocalName == 'srgbClr' else _attr(sub, 'lastClr', value)
        if value:
            found[child.LocalName] = value
    order = ['lt1', 'dk1', 'lt2', 'dk2', 'accent1', 'accent2', 'accent3', 'accent4', 'accent5',
             'accent6', 'hlink', 'folHlink']
    return [found.get(name, DEFAULT_THEME[i]) for i, name in enumerate(order)]


class Palette(object):
    def __init__(self, theme, indexed):
        self.theme = theme
        self.indexed = indexed

    def colour(self, node):
        """(r, g, b) of a <color>/<fgColor> node, None for automatic / none."""
        if node is None or _attr(node, 'auto') in ('1', 'true'):
            return None
        rgb = None
        if _attr(node, 'rgb'):
            rgb = _hex(_attr(node, 'rgb'))
        elif _attr(node, 'theme') is not None:
            index = int(_num(_attr(node, 'theme'), 0))
            rgb = _hex(self.theme[index]) if 0 <= index < len(self.theme) else None
        elif _attr(node, 'indexed') is not None:
            index = int(_num(_attr(node, 'indexed'), 64))
            rgb = _hex(self.indexed[index]) if 0 <= index < len(self.indexed) else None
        if rgb is None:
            return None
        tint = _num(_attr(node, 'tint'), 0.0)
        return _tint(rgb, tint) if tint else rgb


# ============================================================================ number formats
BUILTIN_FORMATS = {
    0: 'General', 1: '0', 2: '0.00', 3: '#,##0', 4: '#,##0.00', 9: '0%', 10: '0.00%',
    11: '0.00E+00', 12: '# ?/?', 13: '# ??/??', 14: 'dd/mm/yyyy', 15: 'd-mmm-yy', 16: 'd-mmm',
    17: 'mmm-yy', 18: 'h:mm AM/PM', 19: 'h:mm:ss AM/PM', 20: 'h:mm', 21: 'h:mm:ss',
    22: 'dd/mm/yyyy h:mm', 37: '#,##0 ;(#,##0)', 38: '#,##0 ;(#,##0)', 39: '#,##0.00;(#,##0.00)',
    40: '#,##0.00;(#,##0.00)', 45: 'mm:ss', 46: '[h]:mm:ss', 47: 'mm:ss.0', 48: '##0.0E+0', 49: '@'}
MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September',
          'October', 'November', 'December']
DAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']


def _sections(code):
    """Split a format code on ';' (not inside quotes)."""
    parts, current, quoted, i = [], [], False, 0
    while i < len(code):
        ch = code[i]
        if ch == '"':
            quoted = not quoted
            current.append(ch)
        elif ch == '\\' and not quoted and i + 1 < len(code):
            current.append(code[i:i + 2])
            i += 1
        elif ch == ';' and not quoted:
            parts.append(u''.join(current))
            current = []
        else:
            current.append(ch)
        i += 1
    parts.append(u''.join(current))
    return parts


def _tokens(section):
    """[('lit', text) | ('code', char)] - quotes, escapes, [colour] tags, _x and *x handled."""
    out, i = [], 0
    while i < len(section):
        ch = section[i]
        if ch == '"':
            end = section.find('"', i + 1)
            end = len(section) if end < 0 else end
            out.append(('lit', section[i + 1:end]))
            i = end + 1
        elif ch == '\\' and i + 1 < len(section):
            out.append(('lit', section[i + 1]))
            i += 2
        elif ch == '_':
            out.append(('lit', u' '))
            i += 2
        elif ch == '*':
            i += 2
        elif ch == '[':
            end = section.find(']', i)
            end = len(section) if end < 0 else end
            tag = section[i + 1:end]
            if re.match(r'^(h+|m+|s+)$', tag, re.IGNORECASE):
                for c in tag:                                   # [h] elapsed time -> plain h
                    out.append(('code', c))
            i = end + 1
        else:
            out.append(('code', ch))
            i += 1
    return out


def general(value):
    """Excel's 'General' look of a number."""
    if value == int(value) and abs(value) < 1e11:
        return u'%d' % int(value)
    text = u'%.10g' % value
    if 'e' in text:
        mantissa, exponent = text.split('e')
        text = u'%sE%s%02d' % (mantissa, '-' if exponent.startswith('-') else '+', abs(int(exponent)))
    return text


def _is_date(tokens):
    for kind, t in tokens:
        if kind == 'code' and t.lower() in ('y', 'd', 'h', 's', 'm'):
            return True
    return False


def _excel_date(serial):
    days = int(serial)
    seconds = int(round((serial - days) * 86400))
    base = datetime.datetime(1899, 12, 30) if days >= 60 else datetime.datetime(1899, 12, 31)
    return base + datetime.timedelta(days=days, seconds=seconds)


def _format_date(value, tokens):
    if value < 0 or value > 2958465:
        return general(value)
    moment = _excel_date(value)
    groups, i = [], 0
    while i < len(tokens):
        kind, t = tokens[i]
        if kind == 'lit':
            groups.append(['lit', t])
            i += 1
            continue
        low = t.lower()
        if low in ('y', 'm', 'd', 'h', 's'):
            j = i
            while j < len(tokens) and tokens[j][0] == 'code' and tokens[j][1].lower() == low:
                j += 1
            groups.append([low, j - i])
            i = j
            continue
        ahead = u''.join(x[1] for x in tokens[i:i + 5] if x[0] == 'code').upper()
        if ahead.startswith('AM/PM'):
            groups.append(['ampm', 0])
            i += 5
            continue
        if ahead.startswith('A/P'):
            groups.append(['ap', 0])
            i += 3
            continue
        groups.append(['lit', t])
        i += 1
    twelve = any(g[0] in ('ampm', 'ap') for g in groups)
    real = [k for k, g in enumerate(groups) if g[0] != 'lit']
    out = []
    for k, (kind, count) in enumerate(groups):
        if kind == 'lit':
            out.append(count)
            continue
        if kind == 'm' and count <= 2:
            position = real.index(k)
            before = groups[real[position - 1]][0] if position > 0 else None
            after = groups[real[position + 1]][0] if position + 1 < len(real) else None
            if before == 'h' or after == 's':
                kind = 'minute'
        if kind == 'y':
            out.append(u'%02d' % (moment.year % 100) if count <= 2 else u'%04d' % moment.year)
        elif kind == 'm':
            name = MONTHS[moment.month - 1]
            out.append({1: u'%d' % moment.month, 2: u'%02d' % moment.month, 3: name[:3],
                        5: name[:1]}.get(count, name))
        elif kind == 'd':
            name = DAYS[moment.weekday()]
            out.append({1: u'%d' % moment.day, 2: u'%02d' % moment.day, 3: name[:3]}.get(count, name))
        elif kind == 'h':
            hour = moment.hour
            if twelve:
                hour = hour % 12 or 12
            out.append(u'%02d' % hour if count >= 2 else u'%d' % hour)
        elif kind == 'minute':
            out.append(u'%02d' % moment.minute if count >= 2 else u'%d' % moment.minute)
        elif kind == 's':
            out.append(u'%02d' % moment.second if count >= 2 else u'%d' % moment.second)
        elif kind == 'ampm':
            out.append(u'AM' if moment.hour < 12 else u'PM')
        elif kind == 'ap':
            out.append(u'A' if moment.hour < 12 else u'P')
    return u''.join(out)


def _group_thousands(digits):
    out = []
    while len(digits) > 3:
        out.insert(0, digits[-3:])
        digits = digits[:-3]
    out.insert(0, digits)
    return u','.join(out)


def _format_number(value, tokens, show_sign):
    places = [i for i, (k, t) in enumerate(tokens) if k == 'code' and t in '0#?']
    if not places:                                  # e.g. a zero section of just "-"
        return u''.join(t for k, t in tokens if k == 'lit' or t not in ',.')
    first, last = places[0], places[-1]
    core = [(k, t) for k, t in tokens[first:last + 1] if k == 'code']
    core_text = u''.join(t for k, t in core)
    if any(k == 'code' and t == '%' for k, t in tokens):
        value *= 100.0
    after = last + 1
    while after < len(tokens) and tokens[after] == ('code', ','):      # trailing commas: / 1000
        value /= 1000.0
        after += 1
    negative = value < 0
    value = abs(value)
    if 'E' in core_text.upper():
        mantissa = re.split(r'[eE]', core_text)[0]
        decimals = len(re.findall(r'[0#?]', mantissa.split('.')[1])) if '.' in mantissa else 0
        text = (u'%.*E' % (decimals, value)).replace('E+0', 'E+').replace('E-0', 'E-')
        exp_digits = len(re.findall(r'0', re.split(r'[eE][+-]?', core_text)[-1])) or 1
        head, exp = text.split('E')
        sign = exp[0] if exp[0] in '+-' else '+'
        text = u'%sE%s%s' % (head, sign, exp.lstrip('+-').zfill(exp_digits))
    else:
        if '/' in core_text:                        # fractions: shown as a decimal
            core_text = core_text.split('/')[0].replace('?', '0') + '.00'
        whole, _, frac = core_text.partition('.')
        decimals = len(re.findall(r'[0#?]', frac))
        min_decimals = len(re.findall(r'0', frac))
        min_whole = len(re.findall(r'0', whole))
        text = u'%.*f' % (decimals, value)
        int_part, _, dec_part = text.partition('.')
        while len(dec_part) > min_decimals and dec_part.endswith('0'):
            dec_part = dec_part[:-1]
        if int_part == '0' and min_whole == 0:
            int_part = u''
        int_part = int_part.zfill(min_whole)
        if ',' in whole and int_part:
            int_part = _group_thousands(int_part)
        text = int_part + (u'.' + dec_part if dec_part else u'')
        if not text:
            text = u'0'
    def literal(part):
        return u''.join(t if k == 'lit' else (u'' if t in ',' else t) for k, t in part)
    prefix = literal(tokens[:first])
    suffix = literal(tokens[after:])
    sign = u'-' if (negative and show_sign and float(text.replace(',', '').split('E')[0] or 0) != 0) else u''
    return sign + prefix + text + suffix


def format_number(value, code):
    """The text Excel shows for a number with this format code."""
    if not code or code.strip().lower() == 'general':
        return general(value)
    sections = _sections(code)
    section, show_sign = sections[0], True
    if len(sections) >= 2 and value < 0:
        section, show_sign = sections[1], False
        value = -value
    elif len(sections) >= 3 and value == 0:
        section = sections[2]
    tokens = _tokens(section)
    codes = u''.join(t for k, t in tokens if k == 'code')
    if 'general' in codes.lower():
        start = codes.lower().index('general')
        out, seen = [], 0
        for kind, t in tokens:
            if kind == 'lit':
                out.append(t)
                continue
            if seen == start:
                out.append(general(value) if show_sign else general(abs(value)))
            if seen < start or seen >= start + 7:
                if t not in ',':
                    out.append(t)
            seen += 1
        return u''.join(out)
    if codes.strip() == '@':
        return general(value)
    if _is_date(tokens):
        return _format_date(value, tokens)
    return _format_number(value, tokens, show_sign)


def format_text(text, code):
    """A text cell with a format that has a text section (4th) or '@' with literals."""
    if not code:
        return text
    sections = _sections(code)
    section = sections[3] if len(sections) >= 4 else (sections[0] if '@' in sections[0] else None)
    if section is None:
        return text
    return u''.join(t if k == 'lit' else (text if t == '@' else u'') for k, t in _tokens(section))


# ============================================================================ cell references
def column_index(letters):
    index = 0
    for ch in letters.upper():
        if 'A' <= ch <= 'Z':
            index = index * 26 + ord(ch) - 64
    return index


def column_letters(index):
    out = u''
    while index > 0:
        index, rest = divmod(index - 1, 26)
        out = chr(65 + rest) + out
    return out


def cell_ref(row, col):
    return u'%s%d' % (column_letters(col), row)


def _split_ref(ref):
    match = re.match(r'^\$?([A-Za-z]+)\$?(\d+)$', ref or '')
    if not match:
        return None
    return int(match.group(2)), column_index(match.group(1))


def parse_range(text, used=None):
    """'A1:H40', 'B3', 'A:H', '3:40', "'Sheet 1'!$A$1:$H$40" -> (r0, c0, r1, c1), 1-based, inclusive."""
    text = (text or u'').strip()
    if '!' in text:
        text = text.split('!')[-1]
    text = text.replace('$', '').replace(' ', '')
    if not text:
        return used
    parts = text.split(':')
    if len(parts) == 1:
        parts = parts * 2
    if len(parts) != 2:
        raise TableError(u'"{0}" is not a cell range. Write it like A1:H40.'.format(text))
    a, b = parts
    if re.match(r'^[A-Za-z]+$', a) and re.match(r'^[A-Za-z]+$', b):        # whole columns
        r0, r1 = (used[0], used[2]) if used else (1, 1)
        return r0, column_index(a), r1, column_index(b)
    if re.match(r'^\d+$', a) and re.match(r'^\d+$', b):                    # whole rows
        c0, c1 = (used[1], used[3]) if used else (1, 1)
        return int(a), c0, int(b), c1
    first, second = _split_ref(a), _split_ref(b)
    if first is None or second is None:
        raise TableError(u'"{0}" is not a cell range. Write it like A1:H40.'.format(text))
    return (min(first[0], second[0]), min(first[1], second[1]),
            max(first[0], second[0]), max(first[1], second[1]))


def range_text(bounds):
    if not bounds:
        return u''
    r0, c0, r1, c1 = bounds
    return u'%s:%s' % (cell_ref(r0, c0), cell_ref(r1, c1))


# ============================================================================ styles
class Font(object):
    __slots__ = ('name', 'size', 'bold', 'italic', 'underline', 'colour')

    def __init__(self, name=u'Calibri', size=11.0, bold=False, italic=False, underline=False, colour=None):
        self.name, self.size, self.bold, self.italic = name, size, bold, italic
        self.underline, self.colour = underline, colour

    def key(self):
        return (self.name, round(self.size, 2), self.bold, self.italic, self.underline, self.colour)


class Style(object):
    __slots__ = ('code', 'font', 'fill', 'borders', 'halign', 'valign', 'wrap', 'indent', 'rotation')

    def __init__(self):
        self.code = 'General'
        self.font = Font()
        self.fill = None                        # (r, g, b) or None
        self.borders = {}                       # 'left'/'right'/'top'/'bottom' -> weight 1..3
        self.halign = 'general'
        self.valign = 'bottom'
        self.wrap = False
        self.indent = 0
        self.rotation = 0


BORDER_WEIGHT = {'hair': 1, 'thin': 1, 'dotted': 1, 'dashed': 1, 'dashDot': 1, 'dashDotDot': 1,
                 'medium': 2, 'mediumDashed': 2, 'mediumDashDot': 2, 'mediumDashDotDot': 2,
                 'slantDashDot': 2, 'thick': 3, 'double': 3}


def _read_styles(root, palette):
    """[Style] by cell style index (cellXfs)."""
    if root is None:
        return [Style()]
    formats = dict(BUILTIN_FORMATS)
    for node in _kids(_kid(root, 'numFmts'), 'numFmt'):
        formats[int(_num(_attr(node, 'numFmtId'), 0))] = _attr(node, 'formatCode', 'General')
    fonts = []
    for node in _kids(_kid(root, 'fonts'), 'font'):
        name = _attr(_kid(node, 'name'), 'val') or _attr(_kid(node, 'rFont'), 'val') or u'Calibri'
        underline = _kid(node, 'u')
        fonts.append(Font(name, _num(_attr(_kid(node, 'sz'), 'val'), 11.0), _flag(node, 'b'),
                          _flag(node, 'i'),
                          underline is not None and (_attr(underline, 'val', 'single') or '') != 'none',
                          palette.colour(_kid(node, 'color'))))
    fills = []
    for node in _kids(_kid(root, 'fills'), 'fill'):
        pattern = _kid(node, 'patternFill')
        colour = None
        if pattern is not None and _attr(pattern, 'patternType') == 'solid':
            colour = palette.colour(_kid(pattern, 'fgColor')) or palette.colour(_kid(pattern, 'bgColor'))
        gradient = _kid(node, 'gradientFill')
        if gradient is not None:                                     # a gradient: its first colour
            stop = _kid(gradient, 'stop')
            colour = palette.colour(_kid(stop, 'color')) if stop is not None else None
        fills.append(colour)
    borders = []
    for node in _kids(_kid(root, 'borders'), 'border'):
        sides = {}
        for side in ('left', 'right', 'top', 'bottom'):
            child = _kid(node, side)
            if child is None:
                child = _kid(node, {'left': 'start', 'right': 'end'}.get(side, side))
            weight = BORDER_WEIGHT.get(_attr(child, 'style') or '', 0)
            if weight:
                sides[side] = weight
        borders.append(sides)
    styles = []
    for xf in _kids(_kid(root, 'cellXfs'), 'xf'):
        style = Style()
        style.code = formats.get(int(_num(_attr(xf, 'numFmtId'), 0)), 'General')
        index = int(_num(_attr(xf, 'fontId'), 0))
        if 0 <= index < len(fonts):
            style.font = fonts[index]
        index = int(_num(_attr(xf, 'fillId'), 0))
        if 0 <= index < len(fills):
            style.fill = fills[index]
        index = int(_num(_attr(xf, 'borderId'), 0))
        if 0 <= index < len(borders):
            style.borders = borders[index]
        align = _kid(xf, 'alignment')
        if align is not None:
            style.halign = _attr(align, 'horizontal', 'general')
            style.valign = _attr(align, 'vertical', 'bottom')
            style.wrap = (_attr(align, 'wrapText', '0') or '0').lower() in ('1', 'true')
            style.indent = int(_num(_attr(align, 'indent'), 0))
            style.rotation = int(_num(_attr(align, 'textRotation'), 0))
        styles.append(style)
    if fonts and not styles:
        styles.append(Style())
    return styles or [Style()]


# ============================================================================ workbook
class SheetInfo(object):
    def __init__(self, name, entry, print_area):
        self.name = name
        self.entry = entry
        self.print_area = print_area            # (r0, c0, r1, c1) or None


class SheetData(object):
    """The raw content of one worksheet."""
    def __init__(self, name):
        self.name = name
        self.cells = {}                          # (row, col) -> (kind, value, style index)
        self.rows = {}                           # row -> (height pt or None, hidden)
        self.cols = []                           # (min, max, width chars or None, hidden)
        self.merges = []                         # (r0, c0, r1, c1)
        self.default_row_pt = 15.0
        self.default_col_width = 9.140625

    def col_info(self, col):
        for low, high, width, hidden in self.cols:
            if low <= col <= high:
                return (width if width is not None else self.default_col_width), hidden
        return self.default_col_width, False

    def row_info(self, row):
        height, hidden = self.rows.get(row, (None, False))
        return (height if height is not None else self.default_row_pt), hidden


def column_mm(width_chars):
    """Excel column width (characters, as stored) -> millimetres (Calibri 11, 7 px digit)."""
    if width_chars <= 0:
        return 0.0
    pixels = int(((256.0 * width_chars + int(128.0 / 7)) / 256.0) * 7)
    return pixels * PX_MM


class Workbook(object):
    def __init__(self, path):
        self.path = path
        self.sheets = []
        self.styles = [Style()]
        self._strings = []
        self._archive = None
        self._csv = None
        if not path or not File.Exists(path):
            raise TableError(u'The file was not found:\n{0}'.format(path))
        extension = (Path.GetExtension(path) or '').lower()
        if extension in ('.xlsx', '.xlsm'):
            self._open_xlsx()
        elif extension == '.csv':
            self._open_csv()
        elif extension == '.xls':
            raise TableError(u'Old .xls workbooks are not supported. Open the file in Excel and '
                             u'save it as .xlsx.')
        else:
            raise TableError(u'"{0}" files are not supported. Choose an .xlsx, .xlsm or .csv file.'
                             .format(extension or u'(no extension)'))
        if not self.sheets:
            raise TableError(u'The workbook has no visible worksheet.')

    # ------------------------------------------------------------------ xlsx
    def _xml(self, name):
        entry = self._archive.GetEntry(name)
        if entry is None:
            return None
        reader = StreamReader(entry.Open(), Encoding.UTF8)
        try:
            document = XmlDocument()
            document.LoadXml(reader.ReadToEnd())
            return document.DocumentElement
        finally:
            reader.Close()

    def _open_xlsx(self):
        try:
            # read the bytes first: works while the workbook is open in Excel
            stream = FileStream(self.path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite)
            try:
                memory = MemoryStream()
                stream.CopyTo(memory)
            finally:
                stream.Close()
            memory.Position = 0
            self._archive = ZipArchive(memory, ZipArchiveMode.Read)
        except Exception as error:
            raise TableError(u'The file could not be read as an Excel workbook:\n{0}'.format(error))
        workbook = self._xml('xl/workbook.xml')
        relations = self._xml('xl/_rels/workbook.xml.rels')
        if workbook is None or relations is None:
            raise TableError(u'This file is not a valid Excel workbook.')
        targets = {}
        for node in _kids(relations, 'Relationship'):
            targets[_attr(node, 'Id')] = _attr(node, 'Target', '')
        theme_target = [t for t in targets.values() if 'theme' in t.lower()]
        theme = _theme_colours(self._xml(self._entry(theme_target[0])) if theme_target else None)
        styles_root = self._xml('xl/styles.xml')
        indexed = list(INDEXED)
        if styles_root is not None:
            custom = _kid(_kid(styles_root, 'colors'), 'indexedColors')
            if custom is not None:
                indexed = [(_attr(n, 'rgb') or '000000')[-6:] for n in _kids(custom, 'rgbColor')] or indexed
        self.styles = _read_styles(styles_root, Palette(theme, indexed))
        self._strings = self._shared_strings()
        sheet_nodes = _kids(_kid(workbook, 'sheets'), 'sheet')
        print_areas = {}
        for node in _kids(_kid(workbook, 'definedNames'), 'definedName'):
            if _attr(node, 'name') == '_xlnm.Print_Area' and _attr(node, 'localSheetId') is not None:
                area = (node.InnerText or u'').split(',')[0]
                try:
                    print_areas[int(_attr(node, 'localSheetId'))] = parse_range(area)
                except TableError:
                    pass
        for position, node in enumerate(sheet_nodes):
            if _attr(node, 'state') in ('hidden', 'veryHidden'):
                continue
            target = targets.get(node.GetAttribute('id', REL_NS))
            if not target:
                continue
            self.sheets.append(SheetInfo(_attr(node, 'name', u'Sheet'), self._entry(target),
                                         print_areas.get(position)))

    @staticmethod
    def _entry(target):
        target = target.replace('\\', '/')
        if target.startswith('/'):
            return target.lstrip('/')
        return target if target.startswith('xl/') else 'xl/' + target

    def _shared_strings(self):
        root = self._xml('xl/sharedStrings.xml')
        strings = []
        for item in _kids(root, 'si'):
            parts = []
            for child in _kids(item):
                if child.LocalName == 't':
                    parts.append(child.InnerText)
                elif child.LocalName == 'r':
                    for sub in _kids(child, 't'):
                        parts.append(sub.InnerText)
            strings.append(u''.join(parts))
        return strings

    def read_sheet(self, name):
        info = [s for s in self.sheets if s.name == name]
        if not info:
            raise TableError(u'Worksheet "{0}" is not in this workbook.'.format(name))
        if self._csv is not None:
            return self._csv
        data = SheetData(name)
        root = self._xml(info[0].entry)
        if root is None:
            return data
        fmt = _kid(root, 'sheetFormatPr')
        if fmt is not None:
            data.default_row_pt = _num(_attr(fmt, 'defaultRowHeight'), 15.0)
            if _attr(fmt, 'defaultColWidth'):
                data.default_col_width = _num(_attr(fmt, 'defaultColWidth'), 9.140625)
            elif _attr(fmt, 'baseColWidth'):
                data.default_col_width = _num(_attr(fmt, 'baseColWidth'), 8.0) + 1.140625
        for node in _kids(_kid(root, 'cols'), 'col'):
            hidden = (_attr(node, 'hidden', '0') or '0').lower() in ('1', 'true')
            data.cols.append((int(_num(_attr(node, 'min'), 1)), int(_num(_attr(node, 'max'), 1)),
                              _num(_attr(node, 'width')), hidden))
        row_number = 0
        for row in _kids(_kid(root, 'sheetData'), 'row'):
            row_number = int(_num(_attr(row, 'r'), row_number + 1))
            hidden = (_attr(row, 'hidden', '0') or '0').lower() in ('1', 'true')
            data.rows[row_number] = (_num(_attr(row, 'ht')), hidden)
            col_number = 0
            for cell in _kids(row, 'c'):
                ref = _split_ref(_attr(cell, 'r'))
                col_number = ref[1] if ref else col_number + 1
                style = int(_num(_attr(cell, 's'), 0))
                data.cells[(row_number, col_number)] = self._cell_value(cell) + (style,)
        for node in _kids(_kid(root, 'mergeCells'), 'mergeCell'):
            try:
                data.merges.append(parse_range(_attr(node, 'ref')))
            except TableError:
                pass
        return data

    def _cell_value(self, cell):
        """(kind, value): kind 'n' number (float), 's' text, 'b' boolean, 'e' error, '' empty."""
        kind = _attr(cell, 't', 'n')
        if kind == 'inlineStr':
            node = _kid(cell, 'is')
            return ('s', node.InnerText if node is not None else u'')
        node = _kid(cell, 'v')
        if node is None:
            return ('', None)
        raw = node.InnerText
        if kind == 's':
            try:
                return ('s', self._strings[int(raw)])
            except (ValueError, IndexError):
                return ('s', u'')
        if kind == 'str':
            return ('s', raw)
        if kind == 'b':
            return ('b', raw.strip() in ('1', 'true'))
        if kind == 'e':
            return ('e', raw)
        if kind == 'd':
            return ('s', raw)
        number = _num(raw)
        return ('n', number) if number is not None else ('s', raw)

    # ------------------------------------------------------------------ csv
    def _open_csv(self):
        try:
            text = File.ReadAllText(self.path, Encoding.UTF8)
        except Exception as error:
            raise TableError(u'The CSV file could not be read:\n{0}'.format(error))
        lines = text.replace('\r\n', '\n').replace('\r', '\n').split('\n')
        while lines and not lines[-1].strip():
            lines.pop()
        first = lines[0] if lines else u''
        delimiter = ';' if first.count(';') > first.count(',') else (u'\t' if first.count('\t') > first.count(',') else ',')
        name = Path.GetFileNameWithoutExtension(self.path)
        data = SheetData(name)
        for r, line in enumerate(lines, 1):
            for c, value in enumerate(_split_csv(line, delimiter), 1):
                if value != u'':
                    number = _num(value) if re.match(r'^-?\d+(\.\d+)?$', value.strip()) else None
                    data.cells[(r, c)] = ('n', number, 0) if number is not None else ('s', value, 0)
        # column widths from the longest text (about one character per Excel width unit)
        longest = {}
        for (r, c), (kind, value, _s) in data.cells.items():
            longest[c] = max(longest.get(c, 0), len(u'{0}'.format(value)))
        for c, length in longest.items():
            data.cols.append((c, c, max(8.43, min(60.0, length * 1.1 + 1.5)), False))
        self._csv = data
        self.sheets.append(SheetInfo(name, None, None))


def _split_csv(line, delimiter):
    cells, current, quoted, i = [], [], False, 0
    while i < len(line):
        ch = line[i]
        if quoted:
            if ch == '"':
                if i + 1 < len(line) and line[i + 1] == '"':
                    current.append('"')
                    i += 1
                else:
                    quoted = False
            else:
                current.append(ch)
        elif ch == '"':
            quoted = True
        elif ch == delimiter:
            cells.append(u''.join(current))
            current = []
        else:
            current.append(ch)
        i += 1
    cells.append(u''.join(current))
    return cells


# ============================================================================ the table
class TextItem(object):
    """One cell's text, where it goes and how it looks."""
    def __init__(self, ref, text, font, halign, valign, wrap, box, indent_mm, room_left, room_right, rotation):
        self.ref = ref
        self.text = text
        self.font = font
        self.halign = halign                     # 'left' / 'center' / 'right'
        self.valign = valign                     # 'top' / 'middle' / 'bottom'
        self.wrap = wrap
        self.box = box                           # (x0, y_top, x1, y_bottom) mm, y negative down
        self.indent_mm = indent_mm
        self.room_left = room_left               # mm of empty cells to the left / right
        self.room_right = room_right             #   (unwrapped text may run into them, as in Excel)
        self.rotation = rotation
        self.span = None                         # (i0, j0, i1, j1) visible row / column indices of the cell
        self.reach_left = None                   # column EDGE index unwrapped text may run to on the left
        self.reach_right = None                  #   ... and on the right (empty neighbour cells)


class Table(object):
    def __init__(self, sheet, bounds):
        self.sheet = sheet
        self.bounds = bounds
        self.xs = []                             # column edges, mm (len = columns + 1)
        self.ys = []                             # row edges, mm, negative down (len = rows + 1)
        self.texts = []
        self.lines = []                          # (x0, y0, x1, y1, weight)
        self.fills = []                          # (x0, y_top, x1, y_bottom, (r, g, b))
        self.grid_lines = []                     # ('h', row edge, col edge from, col edge to, weight) /
                                                 # ('v', col edge, row edge from, row edge to, weight)
        self.grid_fills = []                     # (i0, j0, i1, j1, (r, g, b))
        self.merged = 0
        self.hidden_rows = 0
        self.hidden_cols = 0

    @property
    def width(self):
        return self.xs[-1] if self.xs else 0.0

    @property
    def height(self):
        return -self.ys[-1] if self.ys else 0.0


def _style(wb, index):
    return wb.styles[index] if 0 <= index < len(wb.styles) else wb.styles[0]


def used_range(wb, data):
    """The cells that hold text, a border or a fill (plus merges)."""
    rows, cols = [], []
    for (r, c), (kind, value, style) in data.cells.items():
        st = _style(wb, style)
        if (kind and value not in (None, u'')) or st.fill or st.borders:
            rows.append(r)
            cols.append(c)
    for r0, c0, r1, c1 in data.merges:
        rows += [r0, r1]
        cols += [c0, c1]
    if not rows:
        return None
    return min(rows), min(cols), max(rows), max(cols)


def cell_text(wb, kind, value, style):
    if kind == 'n':
        return format_number(value, style.code)
    if kind == 's':
        return format_text(value or u'', style.code if '@' in (style.code or '') or ';' in (style.code or '') else None)
    if kind == 'b':
        return u'TRUE' if value else u'FALSE'
    if kind == 'e':
        return value or u''
    return u''


def build_table(wb, sheet_name, range_value=u'', skip_hidden=True, borders='excel'):
    """Read a worksheet range into a Table. borders: 'excel' | 'all' | 'none'."""
    data = wb.read_sheet(sheet_name)
    used = used_range(wb, data)
    if used is None:
        raise TableError(u'Worksheet "{0}" is empty.'.format(sheet_name))
    bounds = parse_range(range_value, used)
    r0, c0, r1, c1 = bounds
    if r1 - r0 > 2000 or c1 - c0 > 200:
        raise TableError(u'The range {0} is too big ({1} rows x {2} columns). Choose a smaller range.'
                         .format(range_text(bounds), r1 - r0 + 1, c1 - c0 + 1))
    table = Table(sheet_name, bounds)

    # ---- visible rows / columns and their edges
    rows, cols = [], []
    for r in range(r0, r1 + 1):
        height, hidden = data.row_info(r)
        if hidden and skip_hidden:
            table.hidden_rows += 1
            continue
        if height <= 0:
            continue
        rows.append((r, height * PT_MM))
    for c in range(c0, c1 + 1):
        width, hidden = data.col_info(c)
        if hidden and skip_hidden:
            table.hidden_cols += 1
            continue
        mm = column_mm(width)
        if mm <= 0:
            continue
        cols.append((c, mm))
    if not rows or not cols:
        raise TableError(u'Every row or column of {0} is hidden.'.format(range_text(bounds)))
    row_at = dict((r, i) for i, (r, _h) in enumerate(rows))
    col_at = dict((c, j) for j, (c, _w) in enumerate(cols))
    table.ys = [0.0]
    for _r, height in rows:
        table.ys.append(table.ys[-1] - height)
    table.xs = [0.0]
    for _c, width in cols:
        table.xs.append(table.xs[-1] + width)
    n_rows, n_cols = len(rows), len(cols)

    # ---- merged areas, in visible indices
    anchor_of = {}                                # (i, j) -> (i0, j0, i1, j1) for every cell of a merge
    for m0, n0, m1, n1 in data.merges:
        if m1 < r0 or m0 > r1 or n1 < c0 or n0 > c1:
            continue
        vis_r = [row_at[r] for r in range(max(m0, r0), min(m1, r1) + 1) if r in row_at]
        vis_c = [col_at[c] for c in range(max(n0, c0), min(n1, c1) + 1) if c in col_at]
        if not vis_r or not vis_c:
            continue
        area = (vis_r[0], vis_c[0], vis_r[-1], vis_c[-1])
        if area[0] == area[2] and area[1] == area[3]:
            continue
        table.merged += 1
        for i in range(area[0], area[2] + 1):
            for j in range(area[1], area[3] + 1):
                anchor_of[(i, j)] = area
        # the merge's text and format live in its top-left Excel cell, even when that is clipped
        if (m0, n0) in data.cells and (rows[area[0]][0], cols[area[1]][0]) != (m0, n0):
            data.cells[(rows[area[0]][0], cols[area[1]][0])] = data.cells[(m0, n0)]

    def area_of(i, j):
        return anchor_of.get((i, j), (i, j, i, j))

    # ---- borders: horizontal edge (i, j) = top of row i over column j; vertical (i, j) = left of col j
    h_edges, v_edges = {}, {}

    def put(edges, key, weight):
        if weight > edges.get(key, 0):
            edges[key] = weight

    if borders != 'none':
        if borders == 'all':
            for i in range(n_rows + 1):
                for j in range(n_cols):
                    put(h_edges, (i, j), 1)
            for i in range(n_rows):
                for j in range(n_cols + 1):
                    put(v_edges, (i, j), 1)
        row_edge = {}                              # Excel row -> (top edge index, bottom edge index)
        for r in range(r0 - 1, r1 + 2):
            if r in row_at:
                row_edge[r] = (row_at[r], row_at[r] + 1)
        row_edge[r0 - 1] = (None, 0)
        row_edge[r1 + 1] = (n_rows, None)
        col_edge = {}
        for c in range(c0 - 1, c1 + 2):
            if c in col_at:
                col_edge[c] = (col_at[c], col_at[c] + 1)
        col_edge[c0 - 1] = (None, 0)
        col_edge[c1 + 1] = (n_cols, None)
        for (r, c), (kind, value, style) in data.cells.items():
            if r not in row_edge or c not in col_edge:
                continue
            sides = _style(wb, style).borders
            if not sides:
                continue
            top, bottom = row_edge[r]
            left, right = col_edge[c]
            inside_r, inside_c = r in row_at, c in col_at
            if inside_c:
                j = col_at[c]
                if 'top' in sides and top is not None:
                    put(h_edges, (top, j), sides['top'])
                if 'bottom' in sides and bottom is not None:
                    put(h_edges, (bottom, j), sides['bottom'])
            if inside_r:
                i = row_at[r]
                if 'left' in sides and left is not None:
                    put(v_edges, (i, left), sides['left'])
                if 'right' in sides and right is not None:
                    put(v_edges, (i, right), sides['right'])
            # a merge whose border is stored on its top-left cell only: draw it round the whole area
            if inside_r and inside_c and (row_at[r], col_at[c]) in anchor_of:
                i0, j0, i1, j1 = anchor_of[(row_at[r], col_at[c])]
                if (i0, j0) == (row_at[r], col_at[c]):
                    for j in range(j0, j1 + 1):
                        if 'top' in sides:
                            put(h_edges, (i0, j), sides['top'])
                        if 'bottom' in sides:
                            put(h_edges, (i1 + 1, j), sides['bottom'])
                    for i in range(i0, i1 + 1):
                        if 'left' in sides:
                            put(v_edges, (i, j0), sides['left'])
                        if 'right' in sides:
                            put(v_edges, (i, j1 + 1), sides['right'])
        # no line inside a merged area
        for (i, j), (i0, j0, i1, j1) in anchor_of.items():
            if i > i0:
                h_edges.pop((i, j), None)
            if j > j0:
                v_edges.pop((i, j), None)

    # join neighbouring edge pieces of the same weight into one line
    for i in range(n_rows + 1):
        j = 0
        while j < n_cols:
            weight = h_edges.get((i, j), 0)
            if not weight:
                j += 1
                continue
            k = j
            while k + 1 < n_cols and h_edges.get((i, k + 1), 0) == weight:
                k += 1
            table.lines.append((table.xs[j], table.ys[i], table.xs[k + 1], table.ys[i], weight))
            table.grid_lines.append(('h', i, j, k + 1, weight))      # row edge i, column edges j..k+1
            j = k + 1
    for j in range(n_cols + 1):
        i = 0
        while i < n_rows:
            weight = v_edges.get((i, j), 0)
            if not weight:
                i += 1
                continue
            k = i
            while k + 1 < n_rows and v_edges.get((k + 1, j), 0) == weight:
                k += 1
            table.lines.append((table.xs[j], table.ys[i], table.xs[j], table.ys[k + 1], weight))
            table.grid_lines.append(('v', j, i, k + 1, weight))      # column edge j, row edges i..k+1
            i = k + 1

    # ---- texts and fills
    def has_text(i, j):
        entry = data.cells.get((rows[i][0], cols[j][0]))
        return bool(entry and entry[0] and entry[1] not in (None, u''))

    for i, (r, _h) in enumerate(rows):
        for j, (c, _w) in enumerate(cols):
            i0, j0, i1, j1 = area_of(i, j)
            if (i, j) != (i0, j0):
                continue                                         # inside a merge, not its anchor
            entry = data.cells.get((r, c))
            if entry is None:
                continue
            kind, value, style_index = entry
            style = _style(wb, style_index)
            box = (table.xs[j0], table.ys[i0], table.xs[j1 + 1], table.ys[i1 + 1])
            if style.fill and style.fill != (255, 255, 255):
                table.fills.append(box + (style.fill,))
                table.grid_fills.append((i0, j0, i1, j1, style.fill))
            text = cell_text(wb, kind, value, style)
            text = text.replace(u'\r\n', u'\n').rstrip()
            if not text.strip():
                continue
            halign = style.halign
            if halign in ('general', None):
                halign = 'right' if kind == 'n' else ('center' if kind in ('b', 'e') else 'left')
            elif halign in ('center', 'centerContinuous', 'distributed'):
                halign = 'center'
            elif halign not in ('left', 'right'):
                halign = 'left'                                  # fill / justify
            valign = {'top': 'top', 'center': 'middle', 'distributed': 'middle',
                      'justify': 'top'}.get(style.valign, 'bottom')
            # room for unwrapped text to run into, as Excel does (only over empty, unmerged cells)
            room_left = room_right = 0.0
            reach_left, reach_right = j0, j1 + 1
            if not style.wrap and (i0, j0) not in anchor_of:
                k = j1 + 1
                while k < n_cols and not has_text(i, k) and (i, k) not in anchor_of:
                    room_right += cols[k][1]
                    k += 1
                reach_right = k
                k = j0 - 1
                while k >= 0 and not has_text(i, k) and (i, k) not in anchor_of:
                    room_left += cols[k][1]
                    k -= 1
                reach_left = k + 1
            item = TextItem(cell_ref(r, c), text, style.font, halign, valign,
                            style.wrap or u'\n' in text, box, style.indent * 3 * 7 * PX_MM,
                            room_left, room_right, style.rotation)
            item.span = (i0, j0, i1, j1)
            item.reach_left, item.reach_right = reach_left, reach_right
            table.texts.append(item)
    return table


def describe(wb, sheet_name, range_value=u''):
    """'A1:H40 - 40 rows x 8 columns' (for the window), or the error text."""
    try:
        data = wb.read_sheet(sheet_name)
        used = used_range(wb, data)
        if used is None:
            return None, u'The worksheet is empty.'
        bounds = parse_range(range_value, used)
        return bounds, u'{0}  -  {1} rows x {2} columns'.format(
            range_text(bounds), bounds[2] - bounds[0] + 1, bounds[3] - bounds[1] + 1)
    except TableError as error:
        return None, u'{0}'.format(error)
