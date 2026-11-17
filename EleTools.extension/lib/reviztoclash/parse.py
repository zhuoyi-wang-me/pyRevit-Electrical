# -*- coding: utf-8 -*-
"""Read clash exports into a list of clash dicts. PURE PYTHON (no Revit).

Supported files (the format is detected from the content, not the extension):
    .csv / .txt   comma, semicolon, tab or pipe separated (Excel "CSV UTF-8" too)
    .xlsx / .xlsm Excel workbook - Revizto Workspace "XLS" issue reports are
                  .xlsx files; every sheet is tried, the best header row wins
    .xml          Excel 2003 XML spreadsheet, or a Navisworks XML clash report
    .html / .htm  any HTML table (e.g. Navisworks "HTML (Tabular)" report)
    .bcf / .bcfzip BCF 2.0 / 2.1 / 3.0 - Revizto Issue Tracker export. The
                  clashing elements are the viewpoint's selected Components
                  (AuthoringToolId = Revit element id, IfcGuid = IFC GUID)
Legacy binary .xls (Excel 97-2003) cannot be read without Excel: save it as
.xlsx or CSV first (ParseError says so).

A clash dict:
    {'row': 1-based source row / topic number,
     'clash_id': u'', 'title': u'', 'status': u'', 'test': u'',
     'distance': u'' (as written in the file), 'priority', 'assignee', 'date',
     'a': side, 'b': side, 'extra_ids': int (more elements than A + B)}
    side = {'ids': [int Revit element ids], 'uids': [Revit UniqueIds],
            'ifc': [22-char IFC GUIDs], 'model': u'', 'category': u'', 'name': u''}

Column headers are recognised by their WORDS, so 'Element ID 1',
'Element 1 ID', 'Item 1 Element ID', 'ElementID1', 'Revit ID (A)',
'Element A Id' all mean "Revit element id of side A". See classify().
"""
import re
import sys
import zipfile

PY3 = sys.version_info[0] >= 3

try:
    unicode_type = unicode          # noqa: F821  (IronPython 2.7)
except NameError:                   # CPython 3
    unicode_type = str


class ParseError(Exception):
    """The file cannot be read - message is for the user."""


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def to_text(value):
    if value is None:
        return u""
    if isinstance(value, unicode_type):
        return value
    try:
        return unicode_type(value)
    except Exception:
        try:
            return value.decode("utf-8", "replace")
        except Exception:
            return u"?"


def clean(value):
    return u" ".join(to_text(value).split())


def decode_bytes(data):
    """bytes -> text. UTF-8 (with or without BOM), UTF-16 (BOM) or cp1252."""
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16")
    if data[:3] == b"\xef\xbb\xbf":
        return data[3:].decode("utf-8", "replace")
    try:
        return data.decode("utf-8")
    except Exception:
        return data.decode("cp1252", "replace")


def read_bytes(path):
    with open(path, "rb") as stream:
        return stream.read()


# ---------------------------------------------------------------------------
# element id values
# ---------------------------------------------------------------------------
UNIQUE_ID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                          r"[0-9a-fA-F]{12}-[0-9a-fA-F]{8}$")
GUID_RE = re.compile(r"^\{?[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                     r"[0-9a-fA-F]{12}\}?$")
IFC_RE = re.compile(r"^[0-9A-Za-z_$]{22}$")
INT_RE = re.compile(r"^\d+(?:\.0+)?$")
SCI_RE = re.compile(r"^\d+(?:\.\d+)?[eE]\+?\d+$")
BRACKET_ID_RE = re.compile(r"\[(\d{2,})\]")
SPLIT_RE = re.compile(u"[\\s;,|:/\\\\ ]+")
IFC_CHARS = u"0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_$"


def guid_to_ifc(guid):
    """'2bd5c5b8-....' (36-char GUID) -> 22-char IFC GUID (buildingSMART base64)."""
    hexdigits = re.sub(r"[^0-9a-fA-F]", u"", to_text(guid))
    if len(hexdigits) != 32:
        raise ValueError("not a GUID: {0}".format(guid))
    number = int(hexdigits, 16)
    out = [IFC_CHARS[number >> 126]]
    for shift in range(20, -1, -1):
        out.append(IFC_CHARS[(number >> (6 * shift)) & 63])
    return u"".join(out)


def ifc_to_guid(ifc):
    """22-char IFC GUID -> lower-case 36-char GUID."""
    text = to_text(ifc)
    if len(text) != 22:
        raise ValueError("not an IFC GUID: {0}".format(ifc))
    number = 0
    for ch in text:
        number = number * 64 + IFC_CHARS.index(ch)
    hexdigits = u"{0:032x}".format(number)
    return u"-".join([hexdigits[0:8], hexdigits[8:12], hexdigits[12:16],
                      hexdigits[16:20], hexdigits[20:32]])


def _empty_ids():
    return {"ids": [], "uids": [], "ifc": []}


def parse_ids(value, into=None):
    """Every element identifier in one cell.

    '123456' / '123456.0' / '1.23456E5' / '#123456' / 'ID: 123456'
        -> ids (Revit element id, int)
    'xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx-xxxxxxxx' -> uids (Revit UniqueId)
    'xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx' (GUID) or 22-char IFC GUID -> ifc
    Several values may share one cell, separated by ; , | space or new line.
    """
    out = into if into is not None else _empty_ids()
    for token in SPLIT_RE.split(clean(value)):
        token = token.strip(u"[](){}#\"'<>")
        if not token:
            continue
        if UNIQUE_ID_RE.match(token):
            _add(out["uids"], token.lower())
        elif GUID_RE.match(token):
            _add(out["ifc"], guid_to_ifc(token))
        elif INT_RE.match(token):
            number = int(token.split(u".")[0])
            if number > 0:
                _add(out["ids"], number)
        elif SCI_RE.match(token):
            number = float(token)
            if number > 0 and number == int(number):
                _add(out["ids"], int(number))
        elif IFC_RE.match(token) and not token.isdigit() and not token.isalpha():
            _add(out["ifc"], token)
    return out


def _add(values, value):
    if value not in values:
        values.append(value)


# ---------------------------------------------------------------------------
# column headers
# ---------------------------------------------------------------------------
TOKEN_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")
SIDE_A = set([u"1", u"a", u"first", u"one"])
SIDE_B = set([u"2", u"b", u"second", u"two"])
OBJECT_WORDS = set([u"element", u"elements", u"item", u"items", u"object", u"objects",
                    u"revit", u"entity", u"component", u"components", u"elem", u"selection"])
ID_WORDS = set([u"id", u"ids"])
NAME_WORDS = set([u"element", u"elements", u"item", u"items", u"object", u"objects",
                  u"name", u"family", u"type", u"description", u"entity", u"component"])
MODEL_WORDS = set([u"model", u"models", u"file", u"files", u"document", u"source",
                   u"link", u"discipline", u"rvt"])
ROLE_LABELS = {
    "clash_id": u"Clash ID", "title": u"Title", "status": u"Status", "test": u"Test",
    "distance": u"Distance", "priority": u"Priority", "assignee": u"Assigned to",
    "date": u"Date", "ids": u"Element IDs", "uid": u"GUID / UniqueId", "model": u"Model",
    "category": u"Category", "name": u"Element name", "pair_ids": u"Element IDs (A:B)",
    "group": u"Group", "group_id": u"Group #", "state": u"Clashing status", "point": u"Point",
    "level": u"Level", "gridref": u"Grid",
}
# clash fields filled straight from one column
CLASH_FIELDS = ("clash_id", "title", "status", "test", "distance", "priority", "assignee", "date",
                "group", "group_id", "state", "point", "level", "gridref")


def header_tokens(header):
    text = re.sub(r"(ID|GUID)s\b", r"\1 s", to_text(header))      # 'IDs' -> 'ID', 's'
    return [t.lower() for t in TOKEN_RE.findall(text) if t != u"s"]


def classify(header):
    """Column header -> (role, side) with side 'a', 'b' or None; (None, None)
    when the column means nothing to this tool."""
    tokens = header_tokens(header)
    if not tokens:
        # '#' is the clash number in Revizto clash exports
        return ("clash_id", None) if clean(header) in (u"#", u"No.", u"Nr.") else (None, None)
    words = set(tokens)
    # Revizto clash automation export:
    #   'Authoring tool IDs' = '6113220:9481762' -> Revit element id A : B
    #   'Revizto IDs' are Revizto's own object ids - NOT Revit ids
    #   'Coordinate on alignment', 'XY (distance to alignment)' - civil stationing
    if u"revizto" in words or u"alignment" in words:
        return None, None
    if u"authoring" in words and u"tool" in words:
        side_word = words & (SIDE_A | SIDE_B)
        if side_word:
            return "ids", ("a" if side_word & SIDE_A else "b")
        return "pair_ids", None
    if u"#" in to_text(header) and u"group" in words:
        return "group_id", None
    if words & set([u"coordinates", u"coordinate", u"point", u"position"]):
        # 'Coordinates (m)' preferred; '(ft)' / '(mm)' copies are ignored
        return ("point", None) if not words & set([u"ft", u"feet", u"in", u"mm"]) else (None, None)
    if u"clashing" in words and u"status" in words:
        return "state", None
    if words == set([u"level"]) or words == set([u"level", u"name"]):
        return "level", None
    if words == set([u"grid"]) or words == set([u"grid", u"location"]):
        return "gridref", None
    if u"found" in words and words & set([u"at", u"date", u"on"]):
        return "date", None
    side = None
    if words & SIDE_A and not words & SIDE_B:
        side = "a"
    elif words & SIDE_B and not words & SIDE_A:
        side = "b"
    core = set(t for t in tokens if t not in SIDE_A and t not in SIDE_B)
    if not core:
        return None, None
    clash_word = core & set([u"clash", u"issue", u"topic", u"markup"])

    # --- identifiers --------------------------------------------------------
    if core & set([u"guid", u"guids", u"uniqueid", u"ifcguid"]) or (u"unique" in core and core & ID_WORDS):
        if clash_word and side is None:
            return "clash_id", None
        return "uid", side
    if core & ID_WORDS:
        if clash_word or (core <= ID_WORDS and side is None):
            return "clash_id", None
        if core & set([u"test", u"group", u"stamp", u"user", u"assignee", u"view", u"viewpoint",
                       u"sheet", u"level", u"project", u"model", u"file", u"link", u"comment"]):
            return None, None
        if core & OBJECT_WORDS or core <= ID_WORDS:
            return "ids", side
        return None, None
    if core & set([u"number", u"no", u"nr"]) and (clash_word or len(core) == 1) and side is None:
        return "clash_id", None

    # --- clash level ----------------------------------------------------------
    if u"status" in core or u"state" in core:
        return "status", None
    if core & set([u"test", u"stamp", u"tests"]):
        return "test", None
    if u"group" in core:
        return "group", None
    if core & set([u"distance", u"penetration", u"overlap", u"gap", u"depth", u"clearance"]):
        return "distance", None
    if core & set([u"priority", u"severity"]):
        return "priority", None
    if core & set([u"assigned", u"assignee", u"responsible", u"owner"]):
        return "assignee", None
    if core & set([u"date", u"created", u"modified", u"updated"]) and side is None:
        return "date", None

    # --- per element ----------------------------------------------------------
    if u"category" in core or u"categories" in core:
        return "category", side
    if core & MODEL_WORDS:
        return "model", side
    if side is not None and core <= NAME_WORDS:
        return "name", side
    if side is None and (core & set([u"title", u"summary", u"description", u"name"])
                         or core == set([u"clash"])):
        return "title", None
    return None, None


ELEMENT_ROLES = ("ids", "uid", "pair_ids")


def detect_columns(header_row):
    """{(role, side): column index} for one header row. First column wins, but
    'Parent ...' columns (Revizto: 'Parent group name') only fill roles that no
    other column has (so 'Clash name' is the title, not 'Parent group name')."""
    mapping = {}
    later = []
    for index, header in enumerate(header_row):
        role, side = classify(header)
        if role is None:
            continue
        if u"parent" in header_tokens(header):
            later.append(((role, side), index))
        elif (role, side) not in mapping:
            mapping[(role, side)] = index
    for key, index in later:
        if key not in mapping:
            mapping[key] = index
    return mapping


def score_mapping(mapping):
    score = 0
    for role, _side in mapping:
        score += 3 if role in ELEMENT_ROLES else 1
    return score


def has_element_columns(mapping):
    return any(role in ELEMENT_ROLES for role, _side in mapping)


def find_header(rows, max_scan=30):
    """(row index, mapping) of the most convincing header row in the first
    max_scan rows. Data rows rarely score: their cells are numbers and names."""
    best = (0, {}, -1)
    for index, row in enumerate(rows[:max_scan]):
        if not any(clean(c) for c in row):
            continue
        mapping = detect_columns(row)
        score = score_mapping(mapping) + (5 if has_element_columns(mapping) else 0)
        if score > best[2]:
            best = (index, mapping, score)
    return best[0], best[1]


# ---------------------------------------------------------------------------
# rows -> clashes
# ---------------------------------------------------------------------------
def new_side():
    side = _empty_ids()
    side.update({"model": u"", "category": u"", "name": u"", "hints": []})
    return side


def new_clash(row_number):
    clash = dict((field, u"") for field in CLASH_FIELDS)
    clash.update({"row": row_number, "issue_id": u"", "a": new_side(), "b": new_side(),
                  "extra_ids": 0})
    return clash


ISSUE_STATUS_RE = re.compile(r"^(.*?)\s*\(\s*ID\s*(\d+)\s*\)\s*$", re.I)
VS_RE = re.compile(r"\s+(?:vs\.?|versus|x)\s+", re.I)


def split_status(text):
    """Revizto 'Open (ID 53370)' -> ('Open', '53370')."""
    found = ISSUE_STATUS_RE.match(text or u"")
    if found:
        return found.group(1), found.group(2)
    return text or u"", u""


def split_vs(text):
    """'1S0-SFR-HEA vs MEE_ELQ-SMDB' -> ('1S0-SFR-HEA', 'MEE_ELQ-SMDB'), else None."""
    parts = VS_RE.split(text or u"")
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        return parts[0].strip(), parts[1].strip()
    return None


def excel_date(text):
    """Excel serial date ('46259.425') -> '2026-08-26 10:12'; other text unchanged."""
    try:
        value = float(text)
    except Exception:
        return text
    if not 20000 < value < 80000:
        return text
    import datetime
    stamp = datetime.datetime(1899, 12, 30) + datetime.timedelta(days=value)
    return u"{0:%Y-%m-%d %H:%M}".format(stamp)


def finish_clash(clash):
    """Clean-ups shared by every format: issue id out of the status, A / B
    name hints from 'X vs Y' titles and test names, Excel dates."""
    clash["status"], issue = split_status(clash["status"])
    clash["issue_id"] = clash.get("issue_id") or issue
    clash["date"] = excel_date(clash["date"])
    for text in (clash["title"], clash["test"]):
        pair = split_vs(text)
        if pair:
            for key, hint in zip(("a", "b"), pair):
                if hint not in clash[key]["hints"]:
                    clash[key]["hints"].append(hint)
    return clash


def _cell(row, index):
    if index is None or index >= len(row):
        return u""
    return clean(row[index])


def side_has_ids(side):
    return bool(side["ids"] or side["uids"] or side["ifc"])


def rows_to_clashes(rows, header_index, mapping):
    """Data rows under the header -> clash dicts. Blank rows and repeated
    header rows are skipped."""
    header = [clean(c) for c in rows[header_index]] if rows else []
    clashes = []
    for offset, row in enumerate(rows[header_index + 1:]):
        cells = [clean(c) for c in row]
        if not any(cells):
            continue
        if cells[:len(header)] == header:
            continue
        clash = new_clash(header_index + offset + 2)        # spreadsheet row number
        for (role, side), index in mapping.items():
            value = _cell(cells, index)
            if not value:
                continue
            if role in ("ids", "uid"):
                if side is not None:
                    parse_ids(value, clash[side])
                continue
            if role in ("model", "category", "name"):
                for key in ([side] if side else ["a", "b"]):
                    if not clash[key][role] or side:
                        clash[key][role] = value
                continue
            if role in CLASH_FIELDS:
                clash[role] = value
        # Revizto 'Authoring tool IDs': '6113220:9481762' = A:B
        index = mapping.get(("pair_ids", None))
        if index is not None:
            value = _cell(cells, index)
            if u":" in value:
                left, right = value.split(u":", 1)
                parse_ids(left, clash["a"])
                parse_ids(right, clash["b"])
            else:
                _split_shared(clash, parse_ids(value))
        # one column with both elements ('Element IDs: 123; 456')
        for role in ("ids", "uid"):
            index = mapping.get((role, None))
            if index is not None:
                _split_shared(clash, parse_ids(_cell(cells, index)))
        # last resort: an id in square brackets in the element name ('Wall [123456]')
        for key in ("a", "b"):
            if not side_has_ids(clash[key]) and clash[key]["name"]:
                for match in BRACKET_ID_RE.findall(clash[key]["name"]):
                    _add(clash[key]["ids"], int(match))
        if not clash["clash_id"]:
            clash["clash_id"] = to_text(len(clashes) + 1)
        clashes.append(finish_clash(clash))
    return clashes


def _split_shared(clash, found):
    """Ids of a shared column: first -> A, second -> B (unless already set),
    the rest are counted as extra elements."""
    values = [("ids", v) for v in found["ids"]] + [("uids", v) for v in found["uids"]] + \
             [("ifc", v) for v in found["ifc"]]
    for kind, value in values:
        if not side_has_ids(clash["a"]):
            clash["a"][kind].append(value)
        elif not side_has_ids(clash["b"]):
            clash["b"][kind].append(value)
        else:
            clash["extra_ids"] += 1


# ---------------------------------------------------------------------------
# delimited text
# ---------------------------------------------------------------------------
def sniff_delimiter(text):
    first = u""
    for line in text.splitlines():
        if line.strip():
            first = line
            break
    best, count = u",", 0
    for delimiter in (u",", u";", u"\t", u"|"):
        n = _count_outside_quotes(first, delimiter)
        if n > count:
            best, count = delimiter, n
    return best


def _count_outside_quotes(line, delimiter):
    count, quoted = 0, False
    for ch in line:
        if ch == u'"':
            quoted = not quoted
        elif ch == delimiter and not quoted:
            count += 1
    return count


def parse_delimited(text, delimiter=None):
    """RFC 4180 style: quoted fields, "" escapes, new lines inside quotes."""
    if delimiter is None:
        delimiter = sniff_delimiter(text)
    rows, row, field = [], [], []
    quoted = False
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if quoted:
            if ch == u'"':
                if i + 1 < n and text[i + 1] == u'"':
                    field.append(u'"')
                    i += 1
                else:
                    quoted = False
            else:
                field.append(ch)
        elif ch == u'"' and not field:
            quoted = True
        elif ch == delimiter:
            row.append(u"".join(field))
            field = []
        elif ch in u"\r\n":
            if ch == u"\r" and i + 1 < n and text[i + 1] == u"\n":
                i += 1
            row.append(u"".join(field))
            rows.append(row)
            row, field = [], []
        else:
            field.append(ch)
        i += 1
    if field or row:
        row.append(u"".join(field))
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# XML helpers (namespace-agnostic, IronPython + CPython ElementTree)
# ---------------------------------------------------------------------------
XML_DECL_RE = re.compile(u"^\\s*<\\?xml[^>]*\\?>")
NON_ASCII_RE = re.compile(u"[\ud800-\udbff][\udc00-\udfff]|[^\x00-\x7f]")


def _char_ref(found):
    text = found.group(0)
    if len(text) == 2:                                  # UTF-16 surrogate pair
        code = 0x10000 + ((ord(text[0]) - 0xD800) << 10) + (ord(text[1]) - 0xDC00)
    else:
        code = ord(text)
    return u"&#{0};".format(code)


def _xml(data):
    """Parse the RAW bytes of an XML file.

    CPython: the parser handles BOM and encoding itself.
    IronPython 2.7 (pyRevit) has two traps, both seen with Revizto exports:
      * a byte string IS a unicode string, so re-encoding it turns the UTF-8
        BOM into garbage ('Data at the root level is invalid. Line 1, position 1')
      * raw UTF-8 bytes are parsed but NOT decoded ('–' -> '\xe2\x80\x93'),
        and unicode text with non-ASCII characters raises an ascii codec error.
    So for IronPython: decode ourselves, drop the declaration and hand the
    parser pure ASCII with every other character as an &#NNNN; reference."""
    from xml.etree import ElementTree
    if PY3:
        if isinstance(data, str):
            data = data.encode("utf-8")
    else:
        text = decode_bytes(data)
        data = NON_ASCII_RE.sub(_char_ref, XML_DECL_RE.sub(u"", text.lstrip(u"﻿")))
    try:
        return ElementTree.fromstring(data)
    except Exception as error:
        raise ParseError(u"The XML could not be read: {0}".format(error))


def _local(tag):
    return to_text(tag).rsplit(u"}", 1)[-1]


def _children(element, name):
    return [c for c in list(element) if _local(c.tag) == name]


def _child(element, name):
    for c in list(element):
        if _local(c.tag) == name:
            return c
    return None


def _descendants(element, name):
    return [e for e in element.iter() if _local(e.tag) == name]


def _attr(element, name):
    """Attribute by local name (ignores namespace prefixes)."""
    for key, value in element.attrib.items():
        if _local(key) == name:
            return to_text(value)
    return u""


def _text(element):
    return u"".join(to_text(t) for t in element.itertext()) if element is not None else u""


# ---------------------------------------------------------------------------
# XLSX
# ---------------------------------------------------------------------------
CELL_REF_RE = re.compile(r"^([A-Z]+)(\d+)$")


def _column_index(ref):
    match = CELL_REF_RE.match(to_text(ref).upper())
    if not match:
        return None
    index = 0
    for ch in match.group(1):
        index = index * 26 + (ord(ch) - 64)
    return index - 1


def column_letter(index):
    """0 -> 'A', 25 -> 'Z', 26 -> 'AA' (for the column picker)."""
    name = u""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        name = unichr_(65 + rem) + name
    return name


def unichr_(code):
    try:
        return unichr(code)         # noqa: F821  (IronPython 2.7)
    except NameError:
        return chr(code)


def read_xlsx(archive):
    """[(sheet name, rows)] of every worksheet, in workbook order."""
    names = set(archive.namelist())
    shared = []
    if "xl/sharedStrings.xml" in names:
        root = _xml(archive.read("xl/sharedStrings.xml"))
        for item in _children(root, "si"):
            # rich text runs (r/t); phonetic runs (rPh) are not part of the value
            parts = []
            for node in list(item):
                if _local(node.tag) == "t":
                    parts.append(_text(node))
                elif _local(node.tag) == "r":
                    parts.extend(_text(t) for t in _children(node, "t"))
            shared.append(u"".join(parts))
    targets = {}
    if "xl/_rels/workbook.xml.rels" in names:
        for rel in _xml(archive.read("xl/_rels/workbook.xml.rels")).iter():
            if _local(rel.tag) == "Relationship":
                target = _attr(rel, "Target").lstrip(u"/")
                if not target.startswith(u"xl/"):
                    target = u"xl/" + target
                targets[_attr(rel, "Id")] = target
    sheets = []
    workbook = _xml(archive.read("xl/workbook.xml"))
    for sheet in _descendants(workbook, "sheet"):
        path = targets.get(_attr(sheet, "id"))
        if path and path in names:
            sheets.append((_attr(sheet, "name"), path))
    if not sheets:
        sheets = [(n.rsplit("/", 1)[-1], n) for n in sorted(names)
                  if n.startswith("xl/worksheets/") and n.endswith(".xml")]
    return [(name, _read_sheet(archive.read(path), shared)) for name, path in sheets]


def _read_sheet(data, shared):
    rows = []
    for row in _descendants(_xml(data), "row"):
        values = {}
        position = 0
        for cell in _children(row, "c"):
            index = _column_index(_attr(cell, "r"))
            if index is None:
                index = position
            position = index + 1
            kind = _attr(cell, "t")
            value_node = _child(cell, "v")
            raw = _text(value_node)
            if kind == "s":
                try:
                    value = shared[int(raw)]
                except Exception:
                    value = u""
            elif kind == "inlineStr":
                value = _text(_child(cell, "is"))
            elif kind == "b":
                value = u"TRUE" if raw == u"1" else u"FALSE"
            else:
                value = raw
            values[index] = value
        if values:
            rows.append([values.get(i, u"") for i in range(max(values) + 1)])
        else:
            rows.append([])
    return rows


# ---------------------------------------------------------------------------
# Excel 2003 XML, HTML tables
# ---------------------------------------------------------------------------
def read_spreadsheet_xml(data):
    out = []
    for sheet in _descendants(_xml(data), "Worksheet"):
        rows = []
        for row in _descendants(sheet, "Row"):
            cells = []
            for cell in _children(row, "Cell"):
                index = _attr(cell, "Index")
                if index:
                    while len(cells) < int(index) - 1:
                        cells.append(u"")
                cells.append(_text(_child(cell, "Data")))
            rows.append(cells)
        out.append((_attr(sheet, "Name"), rows))
    return out


TAG_RE = re.compile(r"<[^>]+>")
ROW_RE = re.compile(r"<tr\b.*?</tr\s*>", re.I | re.S)
CELL_RE = re.compile(r"<t[dh]\b.*?(?:</t[dh]\s*>|(?=<t[dh]\b)|$)", re.I | re.S)


def _html_text(fragment):
    text = re.sub(r"<br\s*/?>", u" ", fragment, flags=re.I)
    text = TAG_RE.sub(u"", text)
    for entity, char in ((u"&nbsp;", u" "), (u"&lt;", u"<"), (u"&gt;", u">"),
                         (u"&quot;", u'"'), (u"&#39;", u"'"), (u"&amp;", u"&")):
        text = text.replace(entity, char)
    return clean(text)


def read_html_tables(text):
    """Every <tr> of the page as one row (tables are concatenated)."""
    rows = []
    for row in ROW_RE.findall(text):
        rows.append([_html_text(cell) for cell in CELL_RE.findall(row)])
    return [(u"HTML", rows)]


# ---------------------------------------------------------------------------
# Navisworks XML clash report
# ---------------------------------------------------------------------------
NW_ID_NAMES = (u"element id", u"id", u"revit element id", u"element id value")


def read_navisworks_xml(data):
    root = _xml(data)
    clashes = []
    for test in _descendants(root, "clashtest"):
        test_name = _attr(test, "name")
        for result in _descendants(test, "clashresult"):
            clash = new_clash(len(clashes) + 1)
            clash["clash_id"] = _attr(result, "name") or to_text(len(clashes) + 1)
            clash["title"] = clash["clash_id"]
            clash["status"] = _attr(result, "status")
            clash["distance"] = _attr(result, "distance")
            clash["test"] = test_name
            for node in _descendants(result, "createddate"):
                date = _child(node, "date")
                if date is not None:
                    clash["date"] = u"{0}-{1}-{2}".format(
                        _attr(date, "year"), _attr(date, "month").zfill(2), _attr(date, "day").zfill(2))
                break
            objects = _descendants(result, "clashobject")
            for key, obj in zip(("a", "b"), objects[:2]):
                side = clash[key]
                for attribute in _descendants(obj, "objectattribute"):
                    name = clean(_text(_child(attribute, "name"))).lower()
                    if name in NW_ID_NAMES:
                        parse_ids(_text(_child(attribute, "value")), side)
                nodes = [clean(_text(n)) for n in _descendants(obj, "node")]
                models = [n for n in nodes if re.search(r"\.(rvt|nwc|nwd|ifc)$", n, re.I)]
                side["model"] = models[0] if models else u""
                side["name"] = nodes[-1] if nodes else u""
            clash["extra_ids"] = max(0, len(objects) - 2)
            clashes.append(finish_clash(clash))
    return clashes


# ---------------------------------------------------------------------------
# BCF
# ---------------------------------------------------------------------------
def read_bcf(archive):
    """One clash per BCF topic. Elements A / B are the first two selected
    components of the topic's first viewpoint."""
    by_folder = {}
    for name in archive.namelist():
        parts = name.replace(u"\\", u"/").split(u"/")
        if len(parts) >= 2 and parts[-1]:
            by_folder.setdefault(u"/".join(parts[:-1]), []).append(name)
    clashes = []
    for folder in sorted(by_folder):
        files = by_folder[folder]
        markup = [f for f in files if f.lower().endswith(u"markup.bcf")]
        if not markup:
            continue
        root = _xml(archive.read(markup[0]))
        topic = _child(root, "Topic")
        clash = new_clash(len(clashes) + 1)
        if topic is not None:
            clash["clash_id"] = (clean(_text(_child(topic, "Index"))) or
                                 clean(_text(_child(topic, "ReferenceLink"))) or u"")
            clash["title"] = clean(_text(_child(topic, "Title")))
            clash["status"] = _attr(topic, "TopicStatus")
            # BCF 2.x: <Labels>x</Labels> repeated; BCF 3.0: <Labels><Label>x</Label></Labels>
            labels = [clean(_text(l)) for l in _descendants(topic, "Label")] or \
                     [clean(_text(l)) for l in _children(topic, "Labels")]
            clash["test"] = u"; ".join([l for l in labels if l]) or _attr(topic, "TopicType")
            clash["priority"] = clean(_text(_child(topic, "Priority")))
            clash["assignee"] = clean(_text(_child(topic, "AssignedTo")))
            clash["date"] = clean(_text(_child(topic, "CreationDate")))[:10]
            guid = _attr(topic, "Guid")
            if not clash["clash_id"]:
                clash["clash_id"] = guid[:8] if guid else to_text(len(clashes) + 1)
        # viewpoints in the order the markup lists them, then any others
        listed = [clean(_text(v)) for v in _descendants(root, "Viewpoint")]
        viewpoints = []
        for name in listed:
            for f in files:
                if f.rsplit(u"/", 1)[-1] == name and f not in viewpoints:
                    viewpoints.append(f)
        viewpoints.extend(sorted(f for f in files if f.lower().endswith(u".bcfv") and f not in viewpoints))
        components = []
        for path in viewpoints:
            components = _bcf_components(archive.read(path))
            if components:
                break
        for key, (ifc_guid, tool_id, system) in zip(("a", "b"), components[:2]):
            side = clash[key]
            if tool_id:
                parse_ids(tool_id, side)
            if ifc_guid:
                parse_ids(ifc_guid, side)
            side["name"] = system
        clash["extra_ids"] = max(0, len(components) - 2)
        clashes.append(finish_clash(clash))
    return clashes


def _bcf_components(data):
    """[(IfcGuid, AuthoringToolId, OriginatingSystem)] of the selected components
    (falls back to the coloured ones)."""
    root = _xml(data)
    out = []
    for group in ("Selection", "Coloring"):
        for container in _descendants(root, group):
            for component in _descendants(container, "Component"):
                out.append((_attr(component, "IfcGuid"),
                            clean(_text(_child(component, "AuthoringToolId"))),
                            clean(_text(_child(component, "OriginatingSystem")))))
        if out:
            break
    return out


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------
class ParseResult(object):
    def __init__(self, kind, clashes, sheet=u"", header_row=None, headers=None, mapping=None,
                 rows=None, header_index=0):
        self.kind = kind                    # 'CSV', 'XLSX', 'BCF', ...
        self.clashes = clashes
        self.sheet = sheet
        self.header_row = header_row        # 1-based spreadsheet row, None for BCF / XML
        self.headers = headers or []
        self.mapping = mapping or {}
        self.rows = rows                    # raw rows (tabular only) for remapping
        self.header_index = header_index

    @property
    def tabular(self):
        return self.rows is not None

    def mapping_text(self):
        """'Element IDs (A) = "Element ID 1"; ...' - shown in the window."""
        if not self.tabular:
            return u"{0}: element ids from {1}".format(
                self.kind, u"viewpoint components (AuthoringToolId / IfcGuid)"
                if self.kind == u"BCF" else u"the clash objects")
        parts = []
        order = ["clash_id", "pair_ids", "ids", "uid", "status", "state", "test", "group_id", "group",
                 "title", "model", "category", "name", "level", "gridref", "point", "distance", "date"]
        for (role, side), index in sorted(self.mapping.items(),
                                          key=lambda kv: (order.index(kv[0][0]) if kv[0][0] in order
                                                          else 99, kv[0][1] or u"")):
            label = ROLE_LABELS.get(role, role)
            if side:
                label += u" ({0})".format(side.upper())
            parts.append(u'{0} = "{1}"'.format(label, self.headers[index] if index < len(self.headers) else index))
        return u"; ".join(parts)

    def remap(self, mapping):
        """Rebuild the clashes with a hand-made mapping (from the column picker)."""
        self.mapping = mapping
        self.clashes = rows_to_clashes(self.rows, self.header_index, mapping)
        return self


def _tabular(kind, sheets):
    """Pick the sheet with the best header, build clashes."""
    best = None
    for name, rows in sheets:
        if not rows:
            continue
        index, mapping = find_header(rows)
        score = score_mapping(mapping) + (5 if has_element_columns(mapping) else 0)
        if best is None or score > best[0]:
            best = (score, name, rows, index, mapping)
    if best is None:
        raise ParseError(u"The file has no rows.")
    _score, name, rows, index, mapping = best
    clashes = rows_to_clashes(rows, index, mapping)
    return ParseResult(kind, clashes, sheet=name, header_row=index + 1,
                       headers=[clean(c) for c in rows[index]], mapping=mapping,
                       rows=rows, header_index=index)


def read_file(path):
    """Read any supported clash export. Raises ParseError with a message for
    the user when the file cannot be used."""
    data = read_bytes(path)
    if not data:
        raise ParseError(u"The file is empty.")
    if data[:4] == b"\xd0\xcf\x11\xe0":
        raise ParseError(u"This is an old binary Excel file (.xls, Excel 97-2003). Open it in "
                         u"Excel and save it as .xlsx or CSV UTF-8, then load that file.")
    if data[:2] == b"PK":
        try:
            # open by path: IronPython 2.7's io.BytesIO rejects the bytes read above
            archive = zipfile.ZipFile(path, "r")
        except Exception as error:
            raise ParseError(u"The file looks like a ZIP / XLSX but cannot be opened: {0}".format(error))
        try:                                # always close: Revit keeps running
            names = [n.lower() for n in archive.namelist()]
            if "xl/workbook.xml" in names:
                return _tabular(u"XLSX", read_xlsx(archive))
            if any(n.endswith("markup.bcf") for n in names):
                return ParseResult(u"BCF", read_bcf(archive))
        finally:
            archive.close()
        raise ParseError(u"ZIP file without an Excel workbook or BCF topics.")
    text = decode_bytes(data)
    head = text.lstrip()[:4000].lower()
    if head.startswith(u"<"):
        if u"urn:schemas-microsoft-com:office:spreadsheet" in head:
            return _tabular(u"XML spreadsheet", read_spreadsheet_xml(data))      # raw bytes
        if u"<exchange" in head or u"<clashresult" in text[:200000].lower():
            return ParseResult(u"Navisworks XML", read_navisworks_xml(data))
        if u"<table" in text.lower() or u"<tr" in text.lower():
            return _tabular(u"HTML", read_html_tables(text))
    return _tabular(u"CSV", [(u"", parse_delimited(text))])
