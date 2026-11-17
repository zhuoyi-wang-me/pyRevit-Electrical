# -*- coding: utf-8 -*-
"""Unit tests for lib/reviztoclash (parse, match, report) - no Revit needed.
Run from EleTools.extension:
    py -m unittest discover -s tests/ReviztoClashes
"""
import io
import os
import shutil
import sys
import tempfile
import unittest
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "lib"))

from reviztoclash import match, parse, report  # noqa: E402


# ---------------------------------------------------------------------------
# helpers: build test files in a temp folder
# ---------------------------------------------------------------------------
def _col(index):
    name = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        name = chr(65 + rem) + name
    return name


def make_xlsx(path, sheets, shared=True, bom=False):
    """Minimal .xlsx: sheets = [(name, rows)]. Strings go to sharedStrings
    (like Excel) or inline (like some exporters). bom=True writes every part
    like Revizto does: UTF-8 BOM + XML declaration (this broke IronPython)."""
    strings = []

    class _Zip(zipfile.ZipFile):
        def writestr(self, name, text):
            # every caller passes TEXT; encode exactly once (in IronPython the
            # result of .encode() is a str, not bytes, so no isinstance test)
            data = text.encode("utf-8")
            if bom:
                data = b'\xef\xbb\xbf<?xml version="1.0" encoding="utf-8"?>' + data
            zipfile.ZipFile.writestr(self, name, data)

    with _Zip(path, "w") as z:
        workbook = ['<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>']
        rels = ['<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">']
        for n, (name, rows) in enumerate(sheets, 1):
            workbook.append('<sheet name="{0}" sheetId="{1}" r:id="rId{1}"/>'.format(name, n))
            rels.append('<Relationship Id="rId{0}" Type="worksheet" Target="worksheets/sheet{0}.xml"/>'.format(n))
            xml = ['<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>']
            for r, row in enumerate(rows, 1):
                xml.append('<row r="{0}">'.format(r))
                for c, value in enumerate(row):
                    if value is None or value == "":
                        continue            # gaps, like Excel
                    ref = "{0}{1}".format(_col(c), r)
                    if isinstance(value, (int, float)):
                        xml.append('<c r="{0}"><v>{1}</v></c>'.format(ref, value))
                    elif shared:
                        strings.append(value)
                        xml.append('<c r="{0}" t="s"><v>{1}</v></c>'.format(ref, len(strings) - 1))
                    else:
                        xml.append('<c r="{0}" t="inlineStr"><is><t>{1}</t></is></c>'.format(ref, value))
                xml.append('</row>')
            xml.append('</sheetData></worksheet>')
            z.writestr("xl/worksheets/sheet{0}.xml".format(n), "".join(xml))
        workbook.append('</sheets></workbook>')
        rels.append('</Relationships>')
        z.writestr("xl/workbook.xml", "".join(workbook))
        z.writestr("xl/_rels/workbook.xml.rels", "".join(rels))
        if shared:
            sst = ['<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">']
            for s in strings:
                sst.append('<si><t>{0}</t></si>'.format(s))
            sst.append('</sst>')
            z.writestr("xl/sharedStrings.xml", "".join(sst))


MARKUP = u"""<?xml version="1.0" encoding="UTF-8"?>
<Markup xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <Topic Guid="{guid}" TopicType="Clash" TopicStatus="{status}">
    <Title>{title}</Title>
    <Priority>High</Priority>
    <Index>{index}</Index>
    <Labels>MEP vs STR</Labels>
    <CreationDate>2026-11-02T10:00:00Z</CreationDate>
    <AssignedTo>mep@example.com</AssignedTo>
  </Topic>
  <Viewpoints Guid="v1"><Viewpoint>viewpoint.bcfv</Viewpoint></Viewpoints>
</Markup>"""

VIEWPOINT = u"""<?xml version="1.0" encoding="UTF-8"?>
<VisualizationInfo Guid="v1">
  <Components>
    <Selection>{components}</Selection>
    <Visibility DefaultVisibility="true"/>
  </Components>
</VisualizationInfo>"""

COMPONENT = (u'<Component IfcGuid="{ifc}"><OriginatingSystem>Autodesk Revit 2025</OriginatingSystem>'
             u'<AuthoringToolId>{tool}</AuthoringToolId></Component>')


def make_bcf(path, topics):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("bcf.version", '<Version VersionId="2.1"/>')
        for n, (status, title, comps) in enumerate(topics, 1):
            guid = "0000000{0}-aaaa-bbbb-cccc-dddddddddddd".format(n)
            z.writestr(guid + "/markup.bcf", MARKUP.format(guid=guid, status=status, title=title,
                                                            index=n).encode("utf-8"))
            components = u"".join(COMPONENT.format(ifc=i, tool=t) for i, t in comps)
            z.writestr(guid + "/viewpoint.bcfv", VIEWPOINT.format(components=components).encode("utf-8"))


class TempDir(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, name, text, encoding="utf-8"):
        path = os.path.join(self.tmp, name)
        with io.open(path, "w", encoding=encoding, newline="") as stream:
            stream.write(text)
        return path


# ---------------------------------------------------------------------------
class TestIds(unittest.TestCase):
    def test_numbers(self):
        self.assertEqual(parse.parse_ids(u"123456")["ids"], [123456])
        self.assertEqual(parse.parse_ids(u"123456.0")["ids"], [123456])
        self.assertEqual(parse.parse_ids(u"1.23456E5")["ids"], [123456])
        self.assertEqual(parse.parse_ids(u"#123456")["ids"], [123456])
        self.assertEqual(parse.parse_ids(u"ID: 123456")["ids"], [123456])
        self.assertEqual(parse.parse_ids(u"Element ID:654321")["ids"], [654321])
        self.assertEqual(parse.parse_ids(u"[42]")["ids"], [42])

    def test_several_in_one_cell(self):
        self.assertEqual(parse.parse_ids(u"111; 222, 333\n444 | 111")["ids"], [111, 222, 333, 444])

    def test_rejects_non_ids(self):
        self.assertEqual(parse.parse_ids(u"")["ids"], [])
        self.assertEqual(parse.parse_ids(u"0")["ids"], [])
        self.assertEqual(parse.parse_ids(u"-5")["ids"], [])
        self.assertEqual(parse.parse_ids(u"12.5")["ids"], [])
        self.assertEqual(parse.parse_ids(u"Basic Wall")["ids"], [])

    def test_unique_id_and_guids(self):
        uid = u"60f91daf-3dd7-4283-a86d-24137b73f3da-0001fd0b"
        found = parse.parse_ids(uid.upper())
        self.assertEqual(found["uids"], [uid])
        self.assertEqual(found["ids"], [])
        guid = u"60f91daf-3dd7-4283-a86d-24137b73f3da"
        self.assertEqual(parse.parse_ids(guid)["ifc"], [parse.guid_to_ifc(guid)])
        self.assertEqual(parse.parse_ids(u"{" + guid + u"}")["ifc"], [parse.guid_to_ifc(guid)])
        self.assertEqual(parse.parse_ids(u"1Z_TDl3TH9vhDdcHUdhSgN")["ifc"], [u"1Z_TDl3TH9vhDdcHUdhSgN"])

    def test_ifc_guid_round_trip(self):
        self.assertEqual(parse.guid_to_ifc(u"00000000-0000-0000-0000-000000000000"), u"0" * 22)
        self.assertEqual(parse.guid_to_ifc(u"ffffffff-ffff-ffff-ffff-ffffffffffff"), u"3" + u"$" * 21)
        for guid in (u"60f91daf-3dd7-4283-a86d-24137b73f3da", u"0123abcd-4567-89ef-0123-456789abcdef"):
            ifc = parse.guid_to_ifc(guid)
            self.assertEqual(len(ifc), 22)
            self.assertEqual(parse.ifc_to_guid(ifc), guid)


class TestHeaders(unittest.TestCase):
    def check(self, header, role, side=None):
        self.assertEqual(parse.classify(header), (role, side), header)

    def test_element_id_variants(self):
        for header in (u"Element ID 1", u"Element 1 ID", u"Item 1 Element ID", u"ElementID1",
                       u"Revit ID (A)", u"Element A Id", u"Object 1 Id", u"element_id_1",
                       u"First Element ID"):
            self.check(header, "ids", "a")
        for header in (u"Element ID 2", u"Item 2 Element ID", u"Revit ID (B)", u"ElementID2"):
            self.check(header, "ids", "b")
        for header in (u"Element ID", u"Revit ID", u"Element IDs", u"Revit Element ID"):
            self.check(header, "ids", None)

    def test_guid_columns(self):
        self.check(u"GUID 1", "uid", "a")
        self.check(u"Item 2 GUID", "uid", "b")
        self.check(u"IfcGUID", "uid", None)
        self.check(u"UniqueId A", "uid", "a")
        self.check(u"Clash GUID", "clash_id")

    def test_clash_columns(self):
        self.check(u"ID", "clash_id")
        self.check(u"Clash ID", "clash_id")
        self.check(u"Issue Id", "clash_id")
        self.check(u"No.", "clash_id")
        self.check(u"Status", "status")
        self.check(u"Clash Status", "status")
        self.check(u"Clash Test", "test")
        self.check(u"Test Name", "test")
        self.check(u"Stamp", "test")
        self.check(u"Clash Group", "group")
        self.check(u"Group name", "group")
        self.check(u"Title", "title")
        self.check(u"Clash Name", "title")
        self.check(u"Distance", "distance")
        self.check(u"Penetration", "distance")
        self.check(u"Assigned To", "assignee")
        self.check(u"Priority", "priority")

    def test_element_columns(self):
        self.check(u"Model 1", "model", "a")
        self.check(u"Item 2 Source File", "model", "b")
        self.check(u"Element 1 Category", "category", "a")
        self.check(u"Category B", "category", "b")
        self.check(u"Element 1", "name", "a")
        self.check(u"Item 2 Name", "name", "b")

    def test_ignored(self):
        for header in (u"", u"Comments", u"Test ID", u"Viewpoint ID", u"Item 1 Layer",
                       u"Revizto IDs", u"Coordinates (ft)", u"Coordinate on alignment",
                       u"XY (distance to alignment)", u"Approved at", u"Room"):
            self.assertEqual(parse.classify(header)[0], None, header)

    def test_revizto_clash_sheet_headers(self):
        """Columns of the 'Clashes' sheet of a Revizto clash automation export."""
        self.check(u"#", "clash_id")
        self.check(u"Authoring tool IDs", "pair_ids")
        self.check(u"Authoring tool ID 1", "ids", "a")
        self.check(u"Parent group #", "group_id")
        self.check(u"Parent group name", "group")
        self.check(u"Parent group status", "status")
        self.check(u"Clashing status", "state")
        self.check(u"Clash name", "title")
        self.check(u"Test name", "test")
        self.check(u"Level", "level")
        self.check(u"Grid", "gridref")
        self.check(u"Found at", "date")
        self.check(u"Coordinates (m)", "point")

    def test_parent_columns_only_fill_gaps(self):
        mapping = parse.detect_columns([u"Parent group name", u"Parent group status", u"Clash name",
                                        u"Group name"])
        self.assertEqual(mapping[("group", None)], 3)
        self.assertEqual(mapping[("status", None)], 1)
        self.assertEqual(mapping[("title", None)], 2)

    def test_find_header_below_title_rows(self):
        rows = [[u"Revizto clash report"], [u"Project: Demo"], [],
                [u"ID", u"Status", u"Element ID 1", u"Element ID 2"],
                [u"1", u"Open", u"100", u"200"]]
        index, mapping = parse.find_header(rows)
        self.assertEqual(index, 3)
        self.assertEqual(mapping[("ids", "a")], 2)
        self.assertEqual(mapping[("ids", "b")], 3)


CSV_TEXT = (u"Clash ID,Status,Clash Test,Title,Element ID 1,Model 1,Category 1,Element ID 2,"
            u"Model 2,Category 2,Distance\r\n"
            u"C-001,Active,MEP vs STR,\"Duct, vs beam\",123456,MEP.rvt,Ducts,654321,STR.rvt,"
            u"Structural Framing,-0.052 m\r\n"
            u",,,,,,,,,,\r\n"
            u"C-002,Resolved,MEP vs STR,\"Pipe \"\"A\"\" vs wall\",111,MEP.rvt,Pipes,,,,12 mm\r\n")


class TestCsv(TempDir):
    def test_parse_delimited_quotes(self):
        rows = parse.parse_delimited(u'a,"b,c","d""e"\r\n"multi\nline",2,3\n')
        self.assertEqual(rows, [[u"a", u"b,c", u'd"e'], [u"multi\nline", u"2", u"3"]])

    def test_sniff(self):
        self.assertEqual(parse.sniff_delimiter(u"a;b;c\n1;2;3"), u";")
        self.assertEqual(parse.sniff_delimiter(u"a\tb\tc"), u"\t")
        self.assertEqual(parse.sniff_delimiter(u'"x;y",b,c'), u",")

    def test_read_csv(self):
        result = parse.read_file(self.write("clashes.csv", u"\ufeff" + CSV_TEXT))
        self.assertEqual(result.kind, u"CSV")
        self.assertEqual(len(result.clashes), 2)            # blank row skipped
        first, second = result.clashes
        self.assertEqual(first["clash_id"], u"C-001")
        self.assertEqual(first["title"], u"Duct, vs beam")
        self.assertEqual(first["test"], u"MEP vs STR")
        self.assertEqual(first["a"]["ids"], [123456])
        self.assertEqual(first["a"]["model"], u"MEP.rvt")
        self.assertEqual(first["b"]["ids"], [654321])
        self.assertEqual(first["b"]["category"], u"Structural Framing")
        self.assertEqual(first["row"], 2)
        self.assertEqual(second["title"], u'Pipe "A" vs wall')
        self.assertEqual(second["b"]["ids"], [])
        self.assertIn(u'Element IDs (A) = "Element ID 1"', result.mapping_text())

    def test_semicolon_cp1252(self):
        text = u"Nr;Status;Revit ID (A);Revit ID (B);Titel\r\n7;Neu;10;20;Kabeltrasse \u00fcber T\u00fcr\r\n"
        result = parse.read_file(self.write("eu.csv", text, encoding="cp1252"))
        clash = result.clashes[0]
        self.assertEqual(clash["clash_id"], u"7")
        self.assertEqual((clash["a"]["ids"], clash["b"]["ids"]), ([10], [20]))

    def test_shared_id_column(self):
        text = u"Title,Element IDs\r\nDuct vs beam,111; 222; 333\r\n"
        clash = parse.read_file(self.write("shared.csv", text)).clashes[0]
        self.assertEqual(clash["a"]["ids"], [111])
        self.assertEqual(clash["b"]["ids"], [222])
        self.assertEqual(clash["extra_ids"], 1)
        self.assertEqual(clash["clash_id"], u"1")

    def test_ids_in_name_brackets(self):
        text = u"Title,Element 1,Element 2\r\nX,Basic Wall [1001],Duct [2002]\r\n"
        clash = parse.read_file(self.write("names.csv", text)).clashes[0]
        self.assertEqual((clash["a"]["ids"], clash["b"]["ids"]), ([1001], [2002]))

    def test_no_element_columns_then_remap(self):
        text = u"Ref,Status,Left,Right\r\nR1,Open,5,6\r\n"
        result = parse.read_file(self.write("odd.csv", text))
        self.assertFalse(parse.has_element_columns(result.mapping))
        self.assertEqual(result.headers, [u"Ref", u"Status", u"Left", u"Right"])
        mapping = dict(result.mapping)
        mapping[("ids", "a")] = 2
        mapping[("ids", "b")] = 3
        mapping[("clash_id", None)] = 0
        clash = result.remap(mapping).clashes[0]
        self.assertEqual((clash["clash_id"], clash["a"]["ids"], clash["b"]["ids"]), (u"R1", [5], [6]))

    def test_empty_and_legacy_xls(self):
        self.assertRaises(parse.ParseError, parse.read_file, self.write("empty.csv", u""))
        path = os.path.join(self.tmp, "old.xls")
        with open(path, "wb") as stream:
            stream.write(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 100)
        try:
            parse.read_file(path)
            self.fail("legacy xls must raise")
        except parse.ParseError as error:
            self.assertIn(u".xlsx", u"{0}".format(error))


class TestXlsx(TempDir):
    ROWS = [[u"Revizto issues report"], [],
            [u"ID", u"Title", u"Status", u"Stamp", u"Element ID 1", u"Element ID 2", u"Model 1"],
            [12, u"Duct vs beam", u"Open", u"MEP-STR", 123456, 654321, u"MEP.rvt"],
            [13, u"Tray vs wall", u"Solved", u"MEP-ARC", 222, None, None]]

    def test_shared_strings(self):
        path = os.path.join(self.tmp, "report.xlsx")
        make_xlsx(path, [(u"Cover", [[u"Summary"], [u"nothing here"]]), (u"Issues", self.ROWS)])
        result = parse.read_file(path)
        self.assertEqual((result.kind, result.sheet, result.header_row), (u"XLSX", u"Issues", 3))
        self.assertEqual(len(result.clashes), 2)
        first, second = result.clashes
        self.assertEqual(first["clash_id"], u"12")
        self.assertEqual(first["a"]["ids"], [123456])
        self.assertEqual(first["b"]["ids"], [654321])
        self.assertEqual(first["a"]["model"], u"MEP.rvt")
        self.assertEqual(second["status"], u"Solved")
        self.assertEqual(second["b"]["ids"], [])
        self.assertEqual(first["row"], 4)

    def test_inline_strings_and_gaps(self):
        path = os.path.join(self.tmp, "inline.xlsx")
        make_xlsx(path, [(u"Sheet1", [[u"Clash", None, u"Revit ID (A)", u"Revit ID (B)"],
                                      [u"C1", None, 5, 6]])], shared=False)
        clash = parse.read_file(path).clashes[0]
        self.assertEqual((clash["title"], clash["a"]["ids"], clash["b"]["ids"]), (u"C1", [5], [6]))

    # synthetic copy of the Revizto clash automation workbook layout
    TEST = u"306.STR-Structural Trusses Vs 211.MEP-Electrical Equipment"
    REVIZTO = [
        (u"Clash Tests", [[u"Test name", u"Clash detection status", u"Issue Tracker sync status",
                           u"Clashes", u"Stamp default", u"Assignee default", u"Priority default",
                           u"Date created"],
                          [TEST, u"May be out of date", u"Up to date", u"3", u"(S/M) Structure", u"",
                           u"Critical", u"46200.5"]]),
        (u"Groups", [[u"Test name", u"Group name", u"#", u"Clash count", u"Status", u"Stamp",
                      u"Level", u"Grid", u"Found at"],
                     [TEST, u"L06 - SERVICES No room", u"111", u"2", u"Open (ID 53370)", u"(S/M)",
                      u"L06 - SERVICES", u"L - NW2", u"46259.425"]]),
        (u"Clashes", [[u"Test name", u"Parent group name", u"Parent group #", u"Parent group status",
                       u"Clash name", u"#", u"Clashing status", u"New", u"Level", u"Grid", u"Room",
                       u"Found at", u"Authoring tool IDs", u"Revizto IDs", u"Coordinates (m)",
                       u"Coordinates (ft)", u"Alignment", u"XY (distance to alignment)"],
                      [TEST, u"L06 - SERVICES No room â€“ Ã¼", u"111", u"Open (ID 53370)",
                       u"1S0-SFR-HEA vs MEE_ELQ-SMDB", u"79", u"Clashing", u"", u"L06 - SERVICES",
                       u"L - NW2", u"", u"46259.425002592594", u"6113220:9481762",
                       u"4642667776830950096:-6907528192926278399", u"(671249.56; 2750668.25; 678.71)",
                       u"(2202262.25; 9024502.00; 2226.74)", u"", u""],
                      [TEST, u"L06 - SERVICES No room", u"117", u"Solved (ID 61628)",
                       u"1S0-SFR-HEA vs MEE_ELQ-SMDB", u"82", u"Resolved", u"", u"L06 - SERVICES",
                       u"M - W3", u"", u"46259.4", u"6113221:9481763", u"1:2", u"", u"", u"", u""]]),
    ]

    def test_revizto_clash_workbook_with_bom(self):
        path = os.path.join(self.tmp, u"306.STR-Structural Trusses Vs 211.MEP-Electrical Equipment.xlsx")
        make_xlsx(path, self.REVIZTO, bom=True)
        result = parse.read_file(path)
        self.assertEqual((result.sheet, result.header_row, len(result.clashes)), (u"Clashes", 1, 2))
        first, second = result.clashes
        self.assertEqual(first["clash_id"], u"79")
        self.assertEqual(first["a"]["ids"], [6113220])
        self.assertEqual(first["b"]["ids"], [9481762])        # Revizto IDs ignored
        self.assertEqual((first["status"], first["issue_id"], first["state"]), (u"Open", u"53370", u"Clashing"))
        self.assertEqual((first["group_id"], first["group"]), (u"111", u"L06 - SERVICES No room â€“ Ã¼"))
        self.assertEqual(first["title"], u"1S0-SFR-HEA vs MEE_ELQ-SMDB")
        self.assertEqual(first["test"], self.TEST)
        self.assertEqual((first["level"], first["gridref"]), (u"L06 - SERVICES", u"L - NW2"))
        self.assertEqual(first["point"], u"(671249.56; 2750668.25; 678.71)")
        self.assertEqual(first["date"], u"2026-08-25 10:12")       # Excel epoch 1899-12-30
        self.assertEqual(first["a"]["hints"], [u"1S0-SFR-HEA", u"306.STR-Structural Trusses"])
        self.assertEqual(first["b"]["hints"], [u"MEE_ELQ-SMDB", u"211.MEP-Electrical Equipment"])
        self.assertEqual(report.status_group(second["status"], second["state"]), report.RESOLVED)
        self.assertIn(u'Element IDs (A:B) = "Authoring tool IDs"', result.mapping_text())

    def test_revizto_row(self):
        path = os.path.join(self.tmp, "r.xlsx")
        make_xlsx(path, self.REVIZTO, bom=True)
        clash = parse.read_file(path).clashes[0]
        empty = {"status": match.NOT_FOUND, "refs": [], "note": u""}
        row = report.build_row(0, clash, empty, empty)
        self.assertEqual(row["status"], u"Open / Clashing")
        self.assertEqual(row["group"], report.OPEN)
        self.assertEqual(row["clash_group"], u"#111 L06 - SERVICES No room â€“ Ã¼")
        self.assertEqual(row["location"], u"L06 - SERVICES / L - NW2")
        self.assertTrue(row["notes"].startswith(u"Issue 53370"))

    def test_helpers(self):
        self.assertEqual(parse.split_status(u"Open (ID 53370)"), (u"Open", u"53370"))
        self.assertEqual(parse.split_status(u"Closed"), (u"Closed", u""))
        self.assertEqual(parse.split_vs(u"Duct vs. Beam"), (u"Duct", u"Beam"))
        self.assertEqual(parse.split_vs(u"A Vs B"), (u"A", u"B"))
        self.assertEqual(parse.split_vs(u"Wall"), None)
        self.assertEqual(parse.excel_date(u"abc"), u"abc")
        self.assertEqual(parse.excel_date(u"12"), u"12")

    def test_column_index(self):
        self.assertEqual(parse._column_index(u"A1"), 0)
        self.assertEqual(parse._column_index(u"Z9"), 25)
        self.assertEqual(parse._column_index(u"AA10"), 26)
        self.assertEqual(parse._column_index(u"x"), None)
        for index in (0, 25, 26, 701, 702):
            self.assertEqual(parse._column_index(parse.column_letter(index) + u"1"), index)


class TestOtherFormats(TempDir):
    def test_bcf(self):
        path = os.path.join(self.tmp, "issues.bcfzip")
        guid = u"60f91daf-3dd7-4283-a86d-24137b73f3da"
        ifc = parse.guid_to_ifc(guid)
        make_bcf(path, [(u"Open", u"Duct vs beam", [(ifc, u"123456"), (u"", u"654321")]),
                        (u"Closed", u"Rohr über Tür – café", [(ifc, u"")]),
                        (u"Open", u"Three elements", [(u"", u"1"), (u"", u"2"), (u"", u"3")])])
        result = parse.read_file(path)
        self.assertEqual(result.kind, u"BCF")
        self.assertFalse(result.tabular)
        first, second, third = result.clashes
        self.assertEqual((first["clash_id"], first["title"], first["status"]), (u"1", u"Duct vs beam", u"Open"))
        self.assertEqual(first["test"], u"MEP vs STR")
        self.assertEqual(first["a"]["ids"], [123456])
        self.assertEqual(first["a"]["ifc"], [ifc])
        self.assertEqual(first["b"]["ids"], [654321])
        self.assertEqual(first["date"], u"2026-11-02")
        self.assertEqual(second["title"], u"Rohr über Tür – café")   # non-ASCII intact
        self.assertEqual(second["a"]["ids"], [])
        self.assertEqual(second["a"]["ifc"], [ifc])
        self.assertEqual(second["b"]["ids"], [])
        self.assertEqual(third["extra_ids"], 1)

    def test_html_table(self):
        html = (u"<html><body><table><tr><th>Clash Name</th><th>Status</th><th>Item 1 Element ID</th>"
                u"<th>Item 2 Element ID</th></tr><tr><td>Clash1</td><td>New</td><td>11</td>"
                u"<td>22&nbsp;</td></tr></table></body></html>")
        result = parse.read_file(self.write("report.html", html))
        self.assertEqual(result.kind, u"HTML")
        clash = result.clashes[0]
        self.assertEqual((clash["title"], clash["a"]["ids"], clash["b"]["ids"]), (u"Clash1", [11], [22]))

    def test_spreadsheet_2003_xml(self):
        xml = (u'<?xml version="1.0"?><Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet" '
               u'xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet"><Worksheet ss:Name="Issues"><Table>'
               u'<Row><Cell><Data ss:Type="String">ID</Data></Cell><Cell><Data ss:Type="String">Element ID 1</Data></Cell>'
               u'<Cell ss:Index="4"><Data ss:Type="String">Element ID 2</Data></Cell></Row>'
               u'<Row><Cell><Data ss:Type="Number">3</Data></Cell><Cell><Data ss:Type="Number">77</Data></Cell>'
               u'<Cell ss:Index="4"><Data ss:Type="Number">88</Data></Cell></Row></Table></Worksheet></Workbook>')
        clash = parse.read_file(self.write("report.xml", xml)).clashes[0]
        self.assertEqual((clash["clash_id"], clash["a"]["ids"], clash["b"]["ids"]), (u"3", [77], [88]))

    def test_navisworks_xml(self):
        xml = (u'<?xml version="1.0"?><exchange><batchtest><clashtests>'
               u'<clashtest name="MEP vs STR"><clashresults>'
               u'<clashresult name="Clash1" status="active" distance="-0.052">'
               u'<createddate><date year="2026" month="3" day="7"/></createddate>'
               u'<clashobjects>'
               u'<clashobject><objectattribute><name>Element ID</name><value>123</value></objectattribute>'
               u'<pathlink><node>Plan</node><node>MEP.nwc</node><node>Duct</node></pathlink></clashobject>'
               u'<clashobject><objectattribute><name>Element ID</name><value>456</value></objectattribute>'
               u'<pathlink><node>Plan</node><node>STR.rvt</node><node>Beam</node></pathlink></clashobject>'
               u'</clashobjects></clashresult></clashresults></clashtest></clashtests></batchtest></exchange>')
        result = parse.read_file(self.write("nw.xml", xml))
        self.assertEqual(result.kind, u"Navisworks XML")
        clash = result.clashes[0]
        self.assertEqual((clash["clash_id"], clash["status"], clash["test"]), (u"Clash1", u"active", u"MEP vs STR"))
        self.assertEqual((clash["a"]["ids"], clash["a"]["model"], clash["a"]["name"]), ([123], u"MEP.nwc", u"Duct"))
        self.assertEqual((clash["b"]["ids"], clash["b"]["model"]), ([456], u"STR.rvt"))
        self.assertEqual(clash["date"], u"2026-03-07")


# ---------------------------------------------------------------------------
class FakeSource(object):
    def __init__(self, key, name, elements, is_link=True, doc_key=None):
        self.key = key
        self.name = name
        self.is_link = is_link
        self.doc_key = doc_key or name
        self.elements = elements          # {id: (category, name)}

    def find(self, kind, value):
        if kind == "ids" and value in self.elements:
            cat, name = self.elements[value]
            return value, cat, name
        if kind == "uids" and value in self.elements:
            cat, name = self.elements[value]
            return 999, cat, name
        return None


def side(ids=None, model=u"", category=u"", uids=None):
    s = parse.new_side()
    s["ids"] = list(ids or [])
    s["uids"] = list(uids or [])
    s["model"] = model
    s["category"] = category
    return s


class TestMatch(unittest.TestCase):
    def setUp(self):
        self.host = FakeSource("host", u"MEP_Model", {100: (u"Ducts", u"Rect Duct"),
                                                      300: (u"Walls", u"Host wall")}, is_link=False)
        self.str1 = FakeSource(11, u"STR_Model.rvt", {200: (u"Structural Framing", u"UB 305"),
                                                      300: (u"Structural Columns", u"UC 203")})
        self.str2 = FakeSource(12, u"STR_Model.rvt", {200: (u"Structural Framing", u"UB 305"),
                                                      300: (u"Structural Columns", u"UC 203")})
        self.sources = [self.host, self.str1, self.str2]

    def test_host(self):
        res = match.resolve_side(side([100]), self.sources)
        self.assertEqual(res["status"], match.FOUND)
        self.assertEqual(res["refs"], [("host", 100)])
        self.assertEqual(res["where"], u"MEP_Model")

    def test_link_with_two_instances_is_not_ambiguous(self):
        res = match.resolve_side(side([200]), self.sources)
        self.assertEqual(res["status"], match.FOUND)
        self.assertEqual(res["refs"], [(11, 200)])

    def test_ambiguous_prefers_host(self):
        res = match.resolve_side(side([300]), self.sources)
        self.assertEqual(res["status"], match.AMBIGUOUS)
        self.assertEqual(res["refs"], [("host", 300)])
        self.assertIn(u"STR_Model.rvt", res["note"])

    def test_model_name_decides(self):
        res = match.resolve_side(side([300], model=u"C:\\Proj\\STR_Model_detached.rvt"), self.sources)
        self.assertEqual(res["status"], match.FOUND)
        self.assertEqual(res["refs"], [(11, 300)])

    def test_category_decides(self):
        res = match.resolve_side(side([300], category=u"Structural Columns"), self.sources)
        self.assertEqual((res["status"], res["refs"]), (match.FOUND, [(11, 300)]))

    def test_name_hints_decide(self):
        """Revizto: id 300 exists in host and STR link; the clash name says
        side A is a 'UC 203' column -> the link wins."""
        s = side([300])
        s["hints"] = [u"UC 203", u"306.STR-Structural Trusses"]
        res = match.resolve_side(s, self.sources)
        self.assertEqual((res["status"], res["refs"]), (match.FOUND, [(11, 300)]))
        s["hints"] = [u"Structural Columns"]                       # category in the test name
        self.assertEqual(match.resolve_side(s, self.sources)["refs"], [(11, 300)])
        s["hints"] = [u"zz"]                                       # too short / no match
        self.assertEqual(match.resolve_side(s, self.sources)["status"], match.AMBIGUOUS)

    def test_unknown_model_note(self):
        res = match.resolve_side(side([100], model=u"ELE.rvt"), self.sources)
        self.assertEqual(res["status"], match.FOUND)
        self.assertIn(u"ELE.rvt", res["note"])

    def test_not_found_partial_no_id(self):
        self.assertEqual(match.resolve_side(side([5]), self.sources)["status"], match.NOT_FOUND)
        self.assertEqual(match.resolve_side(side([100, 5]), self.sources)["status"], match.PARTIAL)
        self.assertEqual(match.resolve_side(side(), self.sources)["status"], match.NO_ID)

    def test_same_element_by_id_and_uid_counted_once(self):
        host = FakeSource("host", u"M", {7: (u"Ducts", u"D"), u"uid-7": (u"Ducts", u"D")}, is_link=False)
        host.find = lambda kind, value: (7, u"Ducts", u"D") if value in (7, u"uid-7") else None
        res = match.resolve_side(side([7], uids=[u"uid-7"]), [host])
        self.assertEqual(res["refs"], [("host", 7)])

    def test_model_names(self):
        self.assertTrue(match.model_matches(u"STR_Model.rvt", u"STR_Model"))
        self.assertTrue(match.model_matches(u"str model", u"C:/x/STR_Model_detached.rvt"))
        self.assertTrue(match.model_matches(u"STR_Model.nwc", u"STR_Model_Central.rvt"))
        self.assertFalse(match.model_matches(u"A", u"ARC.rvt"))
        self.assertFalse(match.model_matches(u"", u"ARC.rvt"))
        self.assertEqual(match.model_label(u"C:\\a\\ARC.rvt"), u"ARC")

    def test_clash_found(self):
        self.assertEqual(match.clash_found(match.FOUND, match.AMBIGUOUS), u"Yes")
        self.assertEqual(match.clash_found(match.FOUND, match.NOT_FOUND), u"Partly")
        self.assertEqual(match.clash_found(match.NO_ID, match.NOT_FOUND), u"No")


class TestReport(TempDir):
    def test_status_groups(self):
        for status, group in ((u"Open", report.OPEN), (u"New", report.OPEN), (u"", report.OPEN),
                              (u"Active", report.OPEN), (u"In progress", report.REVIEW),
                              (u"Reviewed", report.REVIEW), (u"Solved", report.RESOLVED),
                              (u"Closed", report.RESOLVED), (u"Approved", report.RESOLVED),
                              (u"Resolved", report.RESOLVED), (u"Weird", report.OTHER)):
            self.assertEqual(report.status_group(status), group, status)

    def test_lengths(self):
        self.assertAlmostEqual(report.length_mm(u"-0.052 m"), -52.0)
        self.assertAlmostEqual(report.length_mm(u"12 mm"), 12.0)
        self.assertAlmostEqual(report.length_mm(u"0,17 ft"), 51.816)
        self.assertAlmostEqual(report.length_mm(u"\u22121.5cm"), -15.0)
        self.assertEqual(report.length_mm(u"0.052"), None)
        self.assertEqual(report.length_mm(u"abc"), None)
        self.assertEqual(report.distance_text(u"-0.052 m"), u"-52 mm")
        self.assertEqual(report.distance_text(u"0.052"), u"0.052")

    def test_points(self):
        self.assertEqual(report.point_text_m((10.0, 0.0, -1.0)), u"X 3.048  Y 0.000  Z -0.305 m")
        self.assertEqual(report.size_text_mm((0, 0, 0), (1.0, 0.5, 0.1)), u"305 x 152 x 30 mm")
        self.assertEqual(report.point_text_m(None), u"")

    def test_row_and_csv(self):
        clash = parse.new_clash(5)
        clash.update({"clash_id": u"C1", "status": u"Active", "title": u'Duct, "big"',
                      "distance": u"-0.05 m"})
        clash["a"]["ids"] = [100]
        clash["b"]["ids"] = [200]
        res_a = {"status": match.FOUND, "where": u"MEP", "label": u"Rect Duct", "category": u"Ducts", "note": u""}
        res_b = {"status": match.NOT_FOUND, "where": u"", "label": u"", "category": u"", "note": u""}
        row = report.build_row(0, clash, res_a, res_b, centre=(0.0, 0.0, 0.0))
        self.assertEqual(row["found"], u"Partly")
        self.assertEqual(row["a_found"], u"Yes - MEP")
        self.assertEqual(row["b_found"], u"No")
        self.assertEqual(row["a_element"], u"Ducts: Rect Duct")
        self.assertEqual(row["distance"], u"-50 mm")
        self.assertEqual(row["group"], report.OPEN)
        path = os.path.join(self.tmp, "out.csv")
        report.write_csv(path, [row])
        with io.open(path, "r", encoding="utf-8-sig", newline="") as stream:
            text = stream.read()
        rows = parse.parse_delimited(text)
        self.assertEqual(rows[0][0], u"Clash ID")
        self.assertEqual(rows[1][0], u"C1")
        title = [label for _k, label in report.CSV_COLUMNS].index(u"Title")
        self.assertEqual(rows[1][title], u'Duct, "big"')
        self.assertEqual(len(rows[1]), len(report.CSV_COLUMNS))


if __name__ == "__main__":
    unittest.main()
