# -*- coding: utf-8 -*-
"""Crash-safe replacement for pyRevit's output.print_table.

WHY
    pyRevit's print_table builds a Markdown table and runs it through its
    Markdown converter. Under IronPython that converter recurses so deeply on
    a table of a few hundred rows that .NET runs out of stack: Revit is killed
    instantly (0xc00000fd in coreclr.dll, no error message, unsaved work lost).
    This happened twice on 2026-11-04 (Room Tag Cleanup, Equipment Power Check).

WHAT
    print_table(output, table_data, columns, formats, title, last_line_style)
    takes the same arguments as output.print_table and draws the same table
    - but as plain HTML through output.print_html (no Markdown converter), in
    chunks of CHUNK rows, at most `limit` rows (a note says how many more).

    Cells keep what Markdown gave them before: linkify() links stay clickable,
    **bold** and `code` are still rendered. Text such as "<No Region>" is now
    shown instead of disappearing as an unknown HTML tag.

USE
    import safetable
    safetable.print_table(output, rows, columns=[...], title='...')
"""
import re

try:
    text_type = unicode        # noqa: F821  (IronPython 2.7)
except NameError:
    text_type = str

MAX_ROWS = 500       # rows drawn per table; the rest is summarised in one line
CHUNK = 100          # rows per HTML block

_BOLD = re.compile(u'\\*\\*(.+?)\\*\\*')
_CODE = re.compile(u'`([^`]+)`')


def _text(value):
    if value is None:
        return u''
    if isinstance(value, text_type):
        return value
    try:
        return text_type(value)
    except Exception:
        try:
            return str(value).decode('utf-8', 'replace')
        except Exception:
            return u'?'


def cell_html(value):
    """One cell as HTML. linkify() output contains no raw < or > (pyRevit
    encodes them), so escaping < and > never breaks a link; '&' is left as it
    is because pyRevit's own encoding uses it."""
    text = _text(value).replace(u'<', u'&lt;').replace(u'>', u'&gt;')
    text = _BOLD.sub(u'<b>\\1</b>', text)
    text = _CODE.sub(u'<code>\\1</code>', text)
    return text.replace(u'\r\n', u'<br>').replace(u'\n', u'<br>')


def build_html(table_data, columns=None, formats=None, title=u'', last_line_style=u'',
               limit=MAX_ROWS, more_note=None):
    """The HTML blocks print_table would print (pure function - unit tested)."""
    rows = [list(r) for r in (table_data or [])]
    columns = list(columns or [])
    formats = list(formats or [])
    width = max([len(r) for r in rows] + [len(columns), 0])
    blocks = []
    if title:
        blocks.append(u'<h3>{0}</h3>'.format(cell_html(title)))
    if not rows and not columns:
        return blocks

    def fmt(index, value):
        f = formats[index] if index < len(formats) else u''
        if f:
            try:
                return f.format(value)
            except Exception:
                return value
        return value

    head = u''
    if columns:
        head = u'<thead><tr>{0}</tr></thead>'.format(u''.join(
            u'<th>{0}</th>'.format(cell_html(columns[i] if i < len(columns) else u''))
            for i in range(width)))
    shown = rows[:limit] if limit else rows
    if not shown:
        blocks.append(u'<table>{0}<tbody></tbody></table>'.format(head))
    last_index = len(rows) - 1
    for start in range(0, len(shown), CHUNK):
        body = []
        for offset, row in enumerate(shown[start:start + CHUNK]):
            index = start + offset
            style = u' style="{0}"'.format(last_line_style) if (last_line_style and index == last_index) else u''
            cells = u''.join(u'<td>{0}</td>'.format(cell_html(fmt(i, row[i] if i < len(row) else u'')))
                             for i in range(width))
            body.append(u'<tr{0}>{1}</tr>'.format(style, cells))
        blocks.append(u'<table>{0}<tbody>{1}</tbody></table>'.format(head if start == 0 else u'', u''.join(body)))
    hidden = len(rows) - len(shown)
    if hidden > 0:
        blocks.append(u'<p><i>... {0} more row(s) not shown{1}.</i></p>'.format(
            hidden, u' - ' + cell_html(more_note) if more_note else u''))
    return blocks


def print_table(output, table_data, columns=None, formats=None, title=u'', last_line_style=u'',
                limit=MAX_ROWS, more_note=None):
    """Same arguments as pyRevit's output.print_table (plus limit / more_note), crash-safe."""
    for block in build_html(table_data, columns, formats, title, last_line_style, limit, more_note):
        output.print_html(block)


def print_line(output, text):
    """A line of text that may hold long linkify() links - without the Markdown converter."""
    output.print_html(u'<p>{0}</p>'.format(cell_html(text)))
