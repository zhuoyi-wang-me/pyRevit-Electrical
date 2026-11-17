# -*- coding: utf-8 -*-
"""Unit tests for lib/sldcheck (rules + checks) - no Revit needed.
Run: py -m unittest discover -s tests/SLDChecker"""
import copy
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..', '..'))
sys.path.insert(0, os.path.join(ROOT, 'lib'))

from sldcheck import checks, config, report, rules  # noqa: E402

CFG = copy.deepcopy(config.DEFAULTS)


class TestRules(unittest.TestCase):
    def test_norm(self):
        self.assertEqual(rules.norm(u"  SMDB-01 – A "), u"smdb-01 - a")
        self.assertEqual(rules.norm(u"DB  01"), rules.norm(u"db 01"))
        self.assertNotEqual(rules.norm(u"DB-01"), rules.norm(u"DB01"))

    def test_current(self):
        self.assertEqual(rules.parse_current(u"63A"), 63)
        self.assertEqual(rules.parse_current(u"4P 100 A"), 100)
        self.assertEqual(rules.parse_current(u"100AT/250AF"), 100)
        self.assertEqual(rules.parse_current(u"1.6kA"), 1600)
        self.assertEqual(rules.parse_current(u"32"), 32)

    def test_power(self):
        self.assertEqual(rules.parse_power(u"250 kVA"), (250, u"kVA"))
        self.assertEqual(rules.parse_power(u"1.2MVA"), (1200, u"kVA"))
        self.assertEqual(rules.parse_power(u"10kW"), (10, u"kW"))
        self.assertEqual(rules.parse_power(u"45"), (45, u"kVA"))
        self.assertEqual(rules.parse_power(u""), (None, None))

    def test_poles(self):
        self.assertEqual(rules.parse_poles(u"TP"), 3)
        self.assertEqual(rules.parse_poles(u"4P"), 3)
        self.assertEqual(rules.parse_poles(u"TPN"), 3)
        self.assertEqual(rules.parse_poles(u"SP+N"), 2)
        self.assertEqual(rules.parse_poles(u"1"), 1)
        self.assertIsNone(rules.parse_poles(u"abc"))

    def test_cable(self):
        c = rules.parse_cable(u"4C x 16mm2 XLPE/SWA/PVC")
        self.assertEqual((c["cores"], c["size_mm2"]), (4, 16))
        self.assertIn(u"xlpe", c["types"])
        c = rules.parse_cable(u"4x240+1x120 E")
        self.assertEqual((c["cores"], c["size_mm2"]), (4, 240))
        c = rules.parse_cable(u"3#16, 1#16, 1#16 E")
        self.assertEqual((c["cores"], c["size_mm2"]), (4, 16))
        c = rules.parse_cable(u"3.5C 240 sq.mm Cu")
        self.assertEqual((c["cores"], c["size_mm2"]), (3.5, 240))

    def test_classify(self):
        r = CFG["classification_rules"]
        self.assertEqual(rules.classify([u"TX-01"], r), u"TX")
        self.assertEqual(rules.classify([u"USMDB-ST-SW-B1-03-01L"], r), u"SMDB")
        self.assertEqual(rules.classify([u"MDB-01"], r), u"MDB")
        self.assertEqual(rules.classify([u"EMDB-G"], r), u"MDB")
        self.assertEqual(rules.classify([u"DB-L1-01"], r), u"DB")

    def test_role(self):
        roles = CFG["sld_roles"]
        self.assertEqual(rules.detail_role(u"S_EC_Panel : Std", {}, roles), u"equipment")
        self.assertEqual(rules.detail_role(u"MCCB_3P : 100A", {}, roles), u"breaker")
        self.assertEqual(rules.detail_role(u"SLD_Cable : Std", {}, roles), u"cable")
        self.assertEqual(rules.detail_role(u"Generic : X", {"cable_size": u"16"}, roles), u"cable")
        self.assertIsNone(rules.detail_role(u"Generic : X", {}, roles))


# ----------------------------------------------------------------------
_ids = iter(range(1000, 100000))


def sld(role, view=1, **f):
    return {"id": next(_ids), "role": role, "label": role, "view": u"SLD %d" % view,
            "view_id": view, "f": f}


def equip(name, fed_from=u"", part=u"", **n):
    return {"id": next(_ids), "kind": "equipment", "label": u"Panel", "part_type": part,
            "f": {"name": name, "fed_from": fed_from}, "n": n}


def circuit(panel, number, load, downstream=None, spare=False, f=None, **n):
    fields = {"circuit_number": number, "load_name": load, "panel": panel}
    fields.update(f or {})
    return {"id": next(_ids), "kind": "circuit", "label": u"cct", "part_type": u"",
            "f": fields, "n": n, "source_panel": panel, "downstream": downstream or [],
            "is_spare": spare, "is_space": False}


def issues(result, check=None):
    return sorted((f["issue"] for f in result.items if check is None or f["check"] == check))


class TestChecks(unittest.TestCase):
    def run_it(self, details, models, whole=True):
        result, stats = checks.run_checks(details, models, CFG, whole)
        return result, stats

    def test_clean_match(self):
        models = [equip(u"MDB-01"), equip(u"DB-01", u"MDB-01", connected_load=50.0),
                  circuit(u"MDB-01", u"1", u"DB-01", [u"DB-01"], breaker_rating=63.0, poles=3.0,
                          f={"cable_size": u"4C x 16mm2"}, connected_load=50.0)]
        details = [sld("equipment", name=u"mdb-01 "), sld("equipment", name=u"DB-01",
                                                          fed_from=u"MDB-01",
                                                          connected_load=u"50 kVA"),
                   sld("breaker", fed_from=u"MDB-01", feeder=u"DB-01", breaker_rating=u"63A",
                       poles=u"TP"),
                   sld("cable", fed_from=u"MDB-01", feeder=u"DB-01", cable_size=u"4C x 16mm2")]
        result, stats = self.run_it(details, models)
        bad = [f for f in result.items if f["severity"] != checks.INFO]
        self.assertEqual(bad, [])
        self.assertEqual(stats["matched_equipment"], 2)
        self.assertEqual(stats["matched_feeders"], 1)
        self.assertEqual(stats["outgoing_feeders"], 1)

    def test_missing_and_duplicates(self):
        models = [equip(u"MDB-01"), equip(u"DB-02", u"MDB-01"), equip(u"DB-03"), equip(u"DB-03")]
        details = [sld("equipment", name=u"MDB-01"), sld("equipment", name=u"MDB-01"),
                   sld("equipment", name=u"DB-99"), sld("equipment", name=u"DB-03")]
        result, _ = self.run_it(details, models)
        found = issues(result, checks.C_MATCH)
        self.assertIn(u"Duplicate Detail Item", found)
        self.assertIn(u"Missing model element", found)
        self.assertIn(u"Missing Detail Item", found)       # DB-02
        self.assertIn(u"Duplicate panel name", found)       # DB-03 x2
        self.assertIn(u"Ambiguous match", found)            # never matched silently

    def test_same_board_in_two_views_is_info(self):
        models = [equip(u"MDB-01")]
        details = [sld("equipment", 1, name=u"MDB-01"), sld("equipment", 2, name=u"MDB-01")]
        result, _ = self.run_it(details, models)
        sev = dict((f["issue"], f["severity"]) for f in result.items)
        self.assertEqual(sev.get(u"Drawn in several views"), checks.INFO)
        self.assertNotIn(u"Duplicate Detail Item", sev)

    def test_hint_but_no_loose_match(self):
        result, _ = self.run_it([sld("equipment", name=u"DB 01")], [equip(u"DB-01")])
        miss = [f for f in result.items if f["issue"] == u"Missing model element"]
        self.assertEqual(len(miss), 1)
        self.assertIn(u"Closest model name", miss[0]["message"])

    def test_breaker_cable_load_mismatch(self):
        models = [equip(u"MDB-01"), equip(u"DB-01", u"MDB-01"),
                  circuit(u"MDB-01", u"3", u"DB-01", [u"DB-01"], breaker_rating=100.0,
                          poles=3.0, design_current=120.0, connected_load=40.0,
                          f={"cable_size": u"4C x 25mm2", "cable_type": u"XLPE/SWA"})]
        details = [sld("equipment", name=u"MDB-01"),
                   sld("breaker", fed_from=u"MDB-01", feeder=u"DB-01", breaker_rating=u"63A",
                       poles=u"SP"),
                   sld("cable", fed_from=u"MDB-01", feeder=u"DB-01", cable_size=u"3C x 16mm2",
                       cable_type=u"PVC", connected_load=u"50kVA")]
        result, _ = self.run_it(details, models)
        found = issues(result)
        for expected in (u"Breaker rating differs", u"Number of poles differs",
                         u"Rating below circuit current", u"Cable size differs",
                         u"Number of cores differs", u"Cable type differs",
                         u"Connected load differs"):
            self.assertIn(expected, found)

    def test_connectivity(self):
        models = [equip(u"MDB-01"), equip(u"SMDB-01", u"MDB-02"), equip(u"MDB-02"),
                  circuit(u"MDB-01", u"5", u"DB-X", [u"DB-X"])]
        details = [sld("equipment", name=u"SMDB-01", fed_from=u"MDB-01"),
                   sld("breaker", fed_from=u"MDB-01", feeder=u"SMDB-01"),
                   sld("breaker", fed_from=u"MDB-01", feeder=u"DB-Y", circuit_number=u"5")]
        result, _ = self.run_it(details, models)
        found = issues(result)
        self.assertIn(u"Source panel differs", found)
        self.assertIn(u"Missing circuit in model", found)
        self.assertIn(u"Circuit feeds a different load", found)

    def test_spares(self):
        models = [equip(u"DB-01"),
                  circuit(u"DB-01", u"7", u"SPARE", spare=True),
                  circuit(u"DB-01", u"8", u"SPARE", spare=True),
                  circuit(u"DB-01", u"9", u"Lights")]
        details = [sld("equipment", name=u"DB-01"),
                   sld("breaker", fed_from=u"DB-01", feeder=u"SPARE", circuit_number=u"7"),
                   sld("breaker", fed_from=u"DB-01", feeder=u"SPARE", circuit_number=u"9")]
        result, _ = self.run_it(details, models)
        found = issues(result, checks.C_BREAKER)
        self.assertIn(u"Spare identification", found)       # cct 9 used in model
        self.assertNotIn(u"Spare count differs", found)     # 2 vs 2

    def test_classification_and_transformer(self):
        models = [equip(u"TX-01", part=u"Transformer"), equip(u"SMDB-02")]
        details = [sld("equipment", name=u"TX-01", classification=u"MDB"),
                   sld("equipment", name=u"SMDB-02", classification=u"DB")]
        result, _ = self.run_it(details, models)
        found = issues(result, checks.C_PANEL)
        self.assertIn(u"Transformer identification", found)
        self.assertIn(u"Classification differs", found)

    def test_partial_scope_missing_only_downstream(self):
        models = [equip(u"MDB-01"), equip(u"DB-01", u"MDB-01"), equip(u"DB-OTHER", u"MDB-09")]
        result, _ = self.run_it([sld("equipment", name=u"MDB-01")], models, whole=False)
        miss = [f["key"] for f in result.items if f["issue"] == u"Missing Detail Item"]
        self.assertEqual(miss, [u"DB-01"])

    def test_switched_off_checks_hidden(self):
        models = [equip(u"MDB-01"), equip(u"DB-02", u"MDB-01")]
        details = [sld("equipment", name=u"MDB-01"), sld("equipment", name=u"MDB-01"),
                   sld("equipment", name=u"DB-99")]
        cfg = copy.deepcopy(CFG)
        cfg["checks"] = {u"match_missing_detail": False, u"match_missing_model": False,
                         u"match_duplicates": False}           # locked -> stays on
        result, _ = checks.run_checks(details, models, cfg, True)
        found = issues(result)
        self.assertNotIn(u"Missing Detail Item", found)
        self.assertNotIn(u"Missing model element", found)
        self.assertIn(u"Duplicate Detail Item", found)
        self.assertEqual(result.hidden, 2)

    def test_every_issue_has_a_toggle_or_is_locked(self):
        for issue in (u"Breaker rating differs", u"Total connected load differs",
                      u"Maximum demand unit differs", u"Missing Detail Item (feeder)",
                      u"Feeder drawn in several views", u"Classification unknown"):
            self.assertIsNotNone(checks.issue_key(issue), issue)

    def test_csv(self):
        result, _ = self.run_it([sld("equipment", name=u'DB, "A"')], [])
        path = os.path.join(tempfile.mkdtemp(), u"out.csv")
        report.write_csv(path, result.sorted())
        with open(path, "rb") as handle:
            data = handle.read().decode("utf-8-sig")
        self.assertIn(u'"DB, ""A"""', data)
        self.assertTrue(data.startswith(u"Check,Severity"))

    def test_board_sections_model(self):
        near = [dict(equip(u"ATS-01"), type_label=u"LV Panel", xyz=(0.0, 0.0, 0.0)),
                dict(equip(u"ATS-01"), type_label=u"LV Panel", xyz=(1.2, 0.0, 0.0))]
        far = [dict(equip(u"AHF"), type_label=u"AHF", xyz=(0.0, 0.0, 0.0)),
               dict(equip(u"AHF"), type_label=u"AHF", xyz=(80.0, 0.0, 0.0))]
        details = [sld("equipment", name=u"ATS-01"), sld("equipment", name=u"AHF")]
        result, stats = self.run_it(details, near + far)
        found = issues(result)
        self.assertIn(u"Board sections (model)", found)
        self.assertEqual(found.count(u"Duplicate panel name"), 1)    # AHF only
        self.assertEqual(found.count(u"Ambiguous match"), 1)         # AHF only
        self.assertEqual(stats["matched_equipment"], 1)              # ATS-01

    def test_board_sections_sld(self):
        details = [dict(sld("equipment", name=u"BR-13"), paper_xy=(0.0, 0.0)),
                   dict(sld("equipment", name=u"BR-13"), paper_xy=(25.0, 0.0))]
        result, _ = self.run_it(details, [equip(u"BR-13")])
        found = issues(result)
        self.assertIn(u"Board sections (SLD)", found)
        self.assertNotIn(u"Duplicate Detail Item", found)
        apart = [dict(sld("equipment", name=u"DB-8"), paper_xy=(0.0, 0.0)),
                 dict(sld("equipment", name=u"DB-8"), paper_xy=(400.0, 0.0))]
        result, _ = self.run_it(apart, [equip(u"DB-8")])
        self.assertIn(u"Duplicate Detail Item", issues(result))
        mixed = [dict(sld("equipment", name=u"DB-9"), paper_xy=(0.0, 0.0)),
                 dict(sld("equipment", name=u"DB-9"), label=u"x", paper_xy=(5.0, 0.0))]
        result, _ = self.run_it(mixed, [equip(u"DB-9")])
        self.assertIn(u"Duplicate Detail Item", issues(result))

    def test_linked_missing_only_when_source_drawn(self):
        linked = [dict(equip(u"LNK-01", u"ELSEWHERE"), linked=True),
                  dict(equip(u"LNK-02", u"MDB-01"), linked=True)]
        details = [sld("equipment", name=u"MDB-01")]
        result, _ = self.run_it(details, [equip(u"MDB-01")] + linked)
        keys = [f["key"] for f in result.items if f["issue"] == u"Missing Detail Item"]
        self.assertEqual(keys, [u"LNK-02"])

    def test_ignore_role(self):
        roles = config.DEFAULTS["sld_roles"]
        self.assertEqual(rules.detail_role(u"MEE_DTI_S_EC_PanelDetail : MEE_DTI_S_EC_PanelDetail",
                                           {}, roles), "ignore")
        self.assertEqual(rules.detail_role(u"MEE_DTI_S_EC_EM_Interlock_SMDB : x", {}, roles), "ignore")
        self.assertEqual(rules.detail_role(u"MEE_DTI_S_EC_Panel : S_EC_Panel", {}, roles), "equipment")

    def test_suffix_fallback(self):
        details = [sld("equipment", name=u"PP-ST-SE-07-02-09L(E)")]
        result, stats = self.run_it(details, [equip(u"PP-ST-SE-07-02-09L")])
        found = issues(result)
        self.assertIn(u"Matched by base name", found)
        self.assertNotIn(u"Missing model element", found)
        self.assertNotIn(u"Missing Detail Item", found)
        self.assertEqual(stats["matched_equipment"], 1)

    def test_unnamed_grouped(self):
        models = [dict(equip(u""), label=u"Battery Rack") for _ in range(5)]
        details = [sld("equipment") for _ in range(4)]
        result, _ = self.run_it(details, models)
        rows = [f for f in result.items if f["issue"] in (u"Model equipment without name",
                                                          u"SLD board without name")]
        self.assertEqual(len(rows), 2)
        model_row = [f for f in rows if f["issue"].startswith(u"Model")][0]
        self.assertEqual(model_row["severity"], checks.INFO)
        self.assertEqual(len(model_row["model_ids"]), 5)

    def test_feeder_pieces(self):
        models = [equip(u"MDB-01"), equip(u"DB-01", u"MDB-01")]
        same = [sld("cable", fed_from=u"MDB-01", feeder=u"DB-01", cable_desc=u"4C 16"),
                sld("cable", fed_from=u"MDB-01", feeder=u"DB-01", cable_desc=u"4C 16")]
        result, _ = self.run_it(same, models)
        found = issues(result)
        self.assertIn(u"Feeder sections (SLD)", found)
        self.assertNotIn(u"Duplicate Detail Item", found)
        differ = [sld("cable", fed_from=u"MDB-01", feeder=u"AHU", cable_desc=u"4C 16"),
                  sld("cable", fed_from=u"MDB-01", feeder=u"AHU", cable_desc=u"4C 25")]
        result, _ = self.run_it(differ, models)
        self.assertIn(u"Duplicate Detail Item", issues(result))

    def test_run_log(self):
        result, stats = self.run_it([sld("equipment", name=u"DB-01")], [])
        cfg = config.complete({})
        path = os.path.join(tempfile.mkdtemp(), u"last_run.txt")
        report.write_log(path, [u"**Model:** test"], stats, {"name": (1, [u"MEE-Component"])},
                         cfg, result.sorted(), result.ok)
        with open(path, "rb") as handle:
            data = handle.read().decode("utf-8")
        self.assertIn(u"Model: test", data)
        self.assertIn(u"name: 1 via MEE-Component", data)
        self.assertIn(u"per issue type:", data)
        self.assertIn(u"DB-01", data)
        # failure-only log (no config / findings yet)
        report.write_log(path, [], None, None, None, None, None, error_text=u"Traceback x")
        with open(path, "rb") as handle:
            self.assertIn(u"FAILED", handle.read().decode("utf-8"))


class TestTemplates(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.saved = (config.CONFIG_DIR, config.TEMPLATE_DIR, config.SETTINGS_PATH,
                      config.CONFIG_PATH)
        config.CONFIG_DIR = self.tmp
        config.TEMPLATE_DIR = os.path.join(self.tmp, "templates")
        config.SETTINGS_PATH = os.path.join(self.tmp, "settings.json")
        config.CONFIG_PATH = os.path.join(self.tmp, "sld_checker_config.json")

    def tearDown(self):
        (config.CONFIG_DIR, config.TEMPLATE_DIR, config.SETTINGS_PATH,
         config.CONFIG_PATH) = self.saved

    def test_save_load_delete(self):
        self.assertEqual(config.list_templates(), [config.DEFAULT_TEMPLATE])
        cfg = copy.deepcopy(CFG)
        cfg["sld_fields"]["breaker_rating"] = [u"MEE-CB Rating °"]
        cfg["checks"] = {u"cable_desc": False}
        config.save_template(u"Project A", cfg)
        self.assertEqual(config.list_templates(), [config.DEFAULT_TEMPLATE, u"Project A"])
        self.assertEqual(config.get_last_template(), u"Project A")
        loaded = config.load_template(u"Project A")
        self.assertEqual(loaded["sld_fields"]["breaker_rating"], [u"MEE-CB Rating °"])
        self.assertFalse(checks.enabled_checks(loaded)[u"cable_desc"])
        config.delete_template(u"Project A")
        self.assertEqual(config.get_last_template(), config.DEFAULT_TEMPLATE)

    def test_partial_template_gets_defaults(self):
        path = os.path.join(self.tmp, "shared.json")
        with open(path, "w") as handle:
            handle.write('{"sld_fields": {"name": ["Board Tag"]}}')
        cfg = config.import_template(path)
        self.assertEqual(cfg["sld_fields"]["name"], [u"Board Tag"])
        self.assertEqual(cfg["sld_fields"]["fed_from"], CFG["sld_fields"]["fed_from"])
        with open(path, "w") as handle:
            handle.write('{"rules": []}')
        self.assertRaises(config.TemplateError, config.import_template, path)

    def test_bad_names(self):
        for name in (u"", u"a/b", config.DEFAULT_TEMPLATE):
            self.assertRaises(config.TemplateError, config.clean_name, name)

    def test_legacy_migration(self):
        with open(config.CONFIG_PATH, "w") as handle:
            handle.write('{"sld_fields": {"poles": ["Pole Count"]}}')
        config.migrate_legacy()
        self.assertIn(u"My mapping", config.list_templates())
        self.assertEqual(config.load_template(u"My mapping")["sld_fields"]["poles"], [u"Pole Count"])

    def test_text_helpers(self):
        self.assertEqual(config.text_to_names(u" A, B ,,A "), [u"A", u"B"])
        text = config.rules_to_text(CFG["classification_rules"])
        self.assertEqual(config.text_to_rules(text), CFG["classification_rules"])
        self.assertRaises(ValueError, config.text_to_rules, u"TX = (")
        self.assertRaises(ValueError, config.text_to_rules, u"no equals sign")

    def test_auto_detect(self):
        names = [u"MEE_Breaker Rating", u"Comments", u"MEE-Poles", u"Cable Size (mm2)"]
        self.assertEqual(config.auto_detect("breaker_rating", names), u"MEE_Breaker Rating")
        self.assertEqual(config.auto_detect("poles", names), u"MEE-Poles")
        self.assertEqual(config.auto_detect("cable_size", names), u"Cable Size (mm2)")
        self.assertIsNone(config.auto_detect("fed_from", names))


if __name__ == "__main__":
    unittest.main()
