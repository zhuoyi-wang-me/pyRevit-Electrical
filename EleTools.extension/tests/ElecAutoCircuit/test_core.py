# -*- coding: utf-8 -*-
"""
Unit tests for the pure-Python core (planner, panel selection, geometry, config,
reporting). Run OUTSIDE Revit:

    cd ElecAutoCircuit.extension
    python -m unittest discover -s tests -v
"""
import copy
import os
import shutil
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'lib'))

from autocircuit import config, reporting                                   # noqa: E402
from autocircuit.geometry import Region, RegionIndex, point_in_polygon       # noqa: E402
from autocircuit.models import (FixtureInfo, PanelInfo, RoomNode, PlannedCircuit, link_adjacency,
                                STATUS_PLANNED, STATUS_NO_PANEL, STATUS_NO_CAPACITY)  # noqa: E402
from autocircuit.panel_selector import apply_region_overrides, filter_panels_to_scope  # noqa: E402
from autocircuit.mapping import sync_mapping                                  # noqa: E402
from autocircuit.planner import CircuitPlanner                               # noqa: E402

_ID = [1000]
# stands in for the project's Electrical Load Classifications
PROJECT_CLASSES = ['Lighting', 'Emergency Lighting', 'Receptacle', 'Power', 'HVAC', 'Other', 'Kitchen Equipment']


def fx(cls='Lighting', va=100.0, room='R1', region='Z1', level=1, x=0.0, y=0.0, poles=1, volt=230.0, **kw):
    _ID[0] += 1
    return FixtureInfo(_ID[0], classification=cls, load_va=va, poles=poles, voltage=volt,
                       point=(x, y, 0.0), level_id=level, level_name='L{0}'.format(level),
                       room_key=room, room_name=room, region=region, **kw)


def panel(name, ptype, region='Z1', max_poles=42, used=0, level=1, x=0.0, y=0.0):
    _ID[0] += 1
    return PanelInfo(_ID[0], name, ptype, level, 'L1', (x, y, 0.0), max_poles, used, [region])


def standard_panels():
    return [panel('LP-1', 'LP'), panel('PP-1', 'PP'), panel('ACP-1', 'ACP')]


class Base(unittest.TestCase):
    def setUp(self):
        PlannedCircuit.reset_sequence()
        self.s = copy.deepcopy(config.load_defaults())
        self.s['classification_map'] = sync_mapping([], PROJECT_CLASSES, self.s['classification_suggestions'])[0]

    def run_plan(self, fixtures, panels=None, rooms=None):
        return CircuitPlanner(self.s).plan(fixtures, panels if panels is not None else standard_panels(),
                                           rooms or {})


class TestPacking(Base):
    def test_lighting_split_by_device_count(self):
        plan = self.run_plan([fx(x=i) for i in range(30)])
        self.assertEqual(sorted(c.count for c in plan.circuits), [6, 12, 12])

    def test_split_by_load_limit(self):
        plan = self.run_plan([fx(va=300, x=i) for i in range(10)])      # 6 x 300 = 1800
        self.assertEqual(sorted(c.count for c in plan.circuits), [4, 6])
        self.assertTrue(all(c.load_va <= 1800 for c in plan.circuits))

    def test_receptacle_limit_is_10(self):
        plan = self.run_plan([fx('Receptacle', va=180, x=i) for i in range(10)])
        self.assertEqual([c.count for c in plan.circuits], [10])

    def test_different_voltage_or_poles_never_share(self):
        plan = self.run_plan([fx('Receptacle', poles=1), fx('Receptacle', poles=2, x=1),
                              fx('Receptacle', volt=120, x=2)])
        self.assertEqual(len(plan.circuits), 3)


class TestDedicated(Base):
    def test_hvac_one_per_circuit(self):
        plan = self.run_plan([fx('HVAC', va=900, x=i) for i in range(3)])
        self.assertEqual(len(plan.circuits), 3)
        self.assertTrue(all(c.panel.panel_type == 'ACP' for c in plan.circuits))

    def test_hvac_override_allows_grouping(self):
        self.s['circuit_rules']['HVAC'].update({'dedicated': False, 'max_devices': 4, 'max_load_va': 5000})
        plan = self.run_plan([fx('HVAC', va=900, x=i) for i in range(3)])
        self.assertEqual(len(plan.circuits), 1)

    def test_equipment_above_threshold_dedicated(self):
        plan = self.run_plan([fx('Other', va=2000), fx('Other', va=500, x=1), fx('Other', va=500, x=2)])
        counts = sorted((c.dedicated, c.count) for c in plan.circuits)
        self.assertEqual(counts, [(False, 2), (True, 1)])

    def test_oversized_device_gets_own_circuit_and_warning(self):
        plan = self.run_plan([fx(va=2500), fx(x=1)])
        self.assertEqual(len(plan.circuits), 2)
        self.assertTrue(any('exceeds' in w for w in plan.warnings))


class TestMerging(Base):
    def rooms(self, adjacent=True):
        a = RoomNode('A', 'A', centroid=(0, 0, 0), boundary_keys=['0:wall1'] if adjacent else [])
        b = RoomNode('B', 'B', centroid=(10, 0, 0), boundary_keys=['0:wall1'] if adjacent else [])
        nodes = {'A': a, 'B': b}
        link_adjacency(nodes)
        return nodes

    def test_small_room_merges_into_adjacent(self):
        fs = [fx(room='A', x=0), fx(room='A', x=1)] + [fx(room='B', x=10 + i) for i in range(3)]
        plan = self.run_plan(fs, rooms=self.rooms())
        self.assertEqual(len(plan.circuits), 1)
        self.assertEqual(plan.circuits[0].merged_rooms, ['A'])
        self.assertEqual(len(plan.merges), 1)

    def test_merging_disabled(self):
        self.s['general']['allow_room_merging'] = False
        fs = [fx(room='A'), fx(room='A', x=1)] + [fx(room='B', x=10 + i) for i in range(3)]
        self.assertEqual(len(self.run_plan(fs, rooms=self.rooms()).circuits), 2)

    def test_no_merge_beyond_distance_when_not_adjacent(self):
        far = [fx(room='B', x=1000 + i) for i in range(3)]      # ~300 m away
        plan = self.run_plan([fx(room='A'), fx(room='A', x=1)] + far, rooms=self.rooms(adjacent=False))
        self.assertEqual(len(plan.circuits), 2)

    def test_no_merge_across_load_classes(self):
        plan = self.run_plan([fx('Lighting', room='A'), fx('Lighting', room='A', x=1),
                              fx('Receptacle', room='B', x=2), fx('Receptacle', room='B', x=3)],
                             rooms=self.rooms())
        self.assertEqual(len(plan.circuits), 2)
        self.assertEqual(plan.merges, [])

    def test_no_merge_across_regions(self):
        panels = standard_panels() + [panel('LP-2', 'LP', region='Z2')]
        plan = self.run_plan([fx(room='A', region='Z1'), fx(room='B', region='Z2', x=1)],
                             panels=panels, rooms=self.rooms())
        self.assertEqual(len(plan.circuits), 2)

    def test_merge_respects_limits(self):
        fs = [fx(room='A'), fx(room='A', x=1)] + [fx(room='B', x=10 + i) for i in range(11)]
        plan = self.run_plan(fs, rooms=self.rooms())                     # 2 + 11 > 12
        self.assertEqual(len(plan.circuits), 2)


class TestPanelSelection(Base):
    def test_lowest_utilisation_wins(self):
        busy, idle = panel('PP-A', 'PP', used=21), panel('PP-B', 'PP', used=8)
        plan = self.run_plan([fx('Receptacle')], panels=[busy, idle])
        self.assertIs(plan.circuits[0].panel, idle)

    def test_load_is_balanced(self):
        self.s['general']['allow_room_merging'] = False
        p1, p2 = panel('PP-A', 'PP'), panel('PP-B', 'PP')
        self.run_plan([fx('Receptacle', room='R{0}'.format(i), x=i * 3) for i in range(4)], panels=[p1, p2])
        self.assertEqual([p1.planned_circuits, p2.planned_circuits], [2, 2])

    def test_no_capacity(self):
        plan = self.run_plan([fx('Receptacle')], panels=[panel('PP-1', 'PP', max_poles=42, used=42)])
        self.assertEqual(plan.circuits[0].status, STATUS_NO_CAPACITY)

    def test_utilisation_cap_keeps_spares(self):
        self.s['general']['max_panel_utilization_pct'] = 80
        plan = self.run_plan([fx('Receptacle')], panels=[panel('PP-1', 'PP', max_poles=10, used=8)])
        self.assertEqual(plan.circuits[0].status, STATUS_NO_CAPACITY)

    def test_missing_panel_type(self):
        plan = self.run_plan([fx('Emergency Lighting')])
        self.assertEqual(plan.circuits[0].status, STATUS_NO_PANEL)

    def test_region_isolation_and_cross_region(self):
        panels = [panel('PP-1', 'PP', region='Z1')]
        plan = self.run_plan([fx('Receptacle', region='Z2')], panels=panels)
        self.assertEqual(plan.circuits[0].status, STATUS_NO_PANEL)
        self.s['general']['allow_cross_region'] = True
        panels = [panel('PP-1', 'PP', region='Z1')]
        plan = self.run_plan([fx('Receptacle', region='Z2')], panels=panels)
        self.assertEqual(plan.circuits[0].status, STATUS_PLANNED)
        self.assertTrue(any('Cross-region' in w for w in plan.warnings))

    def test_region_override(self):
        p = panel('PP-9', 'PP', region='Z1')
        apply_region_overrides([p], {'Z7': ['pp-9']})
        self.assertEqual(p.regions, set(['Z7']))

    def test_locked_panel_excluded(self):
        p = panel('PP-1', 'PP')
        p.locked = True
        self.assertEqual(self.run_plan([fx('Receptacle')], panels=[p]).circuits[0].status, STATUS_NO_PANEL)


class TestClassification(Base):
    def test_unmapped_and_missing_are_skipped(self):
        plan = self.run_plan([fx('Unicorn Load'), fx(cls='')])
        self.assertEqual(len(plan.circuits), 0)
        self.assertEqual(len(plan.skipped), 2)

    def test_already_circuited_skipped(self):
        plan = self.run_plan([fx(already_circuited=True)])
        self.assertEqual(plan.skipped[0][1], 'Already connected to a circuit')

    def test_case_insensitive_and_wildcard(self):
        self.s['classification_map'].append({'classification': 'lighting*', 'panel_type': 'LP', 'rule': 'Lighting'})
        plan = self.run_plan([fx('LIGHTING'), fx('Lighting - Exterior', x=1)])
        self.assertTrue(all(c.panel_type == 'LP' for c in plan.circuits))
        self.assertEqual(len(plan.skipped), 0)


class TestProjectMapping(Base):
    def test_every_project_classification_gets_a_row(self):
        rows = self.s['classification_map']
        self.assertEqual([r['classification'] for r in rows], PROJECT_CLASSES)
        self.assertTrue(all(r['in_project'] for r in rows))
        by = dict((r['classification'], (r['panel_type'], r['rule'])) for r in rows)
        self.assertEqual(by['Emergency Lighting'], ('ELP', 'Lighting'))
        self.assertEqual(by['Lighting'], ('LP', 'Lighting'))
        self.assertEqual(by['HVAC'], ('ACP', 'HVAC'))
        self.assertEqual(by['Receptacle'], ('PP', 'Receptacle'))
        self.assertEqual(by['Other'], ('PP', 'Equipment'))

    def test_user_choices_preserved_and_missing_flagged(self):
        saved = [{'classification': 'LIGHTING', 'panel_type': 'DB-L', 'rule': 'Lighting'},
                 {'classification': 'Old Class', 'panel_type': 'PP', 'rule': 'Equipment'},
                 {'classification': 'Light*', 'panel_type': 'LP', 'rule': 'Lighting'}]
        rows, added, missing = sync_mapping(saved, ['Lighting', 'Motor'], self.s['classification_suggestions'])
        by = dict((r['classification'], r) for r in rows)
        self.assertEqual(by['Lighting']['panel_type'], 'DB-L')      # user's choice kept, project spelling
        self.assertEqual(added, ['Motor'])
        self.assertEqual(by['Motor']['panel_type'], 'ACP')
        self.assertEqual(missing, ['Old Class'])                   # wildcard is not reported missing
        self.assertFalse(by['Old Class']['in_project'])

    def test_blank_panel_type_excludes(self):
        for r in self.s['classification_map']:
            if r['classification'] == 'Other':
                r['panel_type'] = ''
        self.assertEqual(config.validate(self.s), [])
        plan = self.run_plan([fx('Other')])
        self.assertEqual(plan.circuits, [])
        self.assertIn('excluded', plan.skipped[0][1])


class TestSelectedRegionScope(Base):
    def test_scope_requires_a_selected_region(self):
        self.s['scope'] = {'mode': 'selected_regions', 'selected_region_ids': [], 'inclusion': 'fixture'}
        self.assertTrue(any('no filled region is selected' in e for e in config.validate(self.s)))
        self.assertEqual(config.validate(self.s, check_scope=False), [])   # allowed while picking
        self.s['scope']['selected_region_ids'] = ['abc-123']
        self.assertEqual(config.validate(self.s), [])

    def test_only_panels_of_selected_regions_are_kept(self):
        inside = panel('LP-A', 'LP', region='Zone A')
        outside = panel('LP-X', 'LP', region='<No Region>')
        overridden = panel('LP-E', 'LP', region='<No Region>')      # e.g. in an electrical room outside
        apply_region_overrides([inside, outside, overridden], {'Zone A': ['LP-E']})
        kept = filter_panels_to_scope([inside, outside, overridden], ['Zone A'])
        self.assertEqual([p.name for p in kept], ['LP-A', 'LP-E'])

    def test_devices_outside_selected_region_located_as_none(self):
        idx = RegionIndex([Region('Zone A', [[(0, 0), (50, 0), (50, 50), (0, 50)]], level_id=1)])
        self.assertEqual(idx.locate(10, 10, 1), 'Zone A')
        self.assertIsNone(idx.locate(80, 10, 1))                    # outside -> skipped by collector
        self.assertIsNone(idx.locate(10, 10, 2))                    # other level -> skipped
        self.assertEqual(idx.outline(), (0.0, 0.0, 50.0, 50.0))     # bounding-box pre-filter

    def test_in_scope_plan_uses_only_scope_panels(self):
        fs = [fx(region='Zone A', x=i) for i in range(5)]
        kept = filter_panels_to_scope([panel('LP-A', 'LP', region='Zone A'), panel('LP-B', 'LP', region='Zone B')],
                                      ['Zone A'])
        plan = self.run_plan(fs, panels=kept)
        self.assertEqual(len(plan.circuits), 1)
        self.assertEqual(plan.circuits[0].panel.name, 'LP-A')

    def test_global_settings_file_does_not_keep_selection(self):
        tmp = tempfile.mkdtemp()
        old_app, old_global = config.APP_DIR, config.GLOBAL_FILE
        try:
            config.APP_DIR = tmp
            config.GLOBAL_FILE = os.path.join(tmp, 'settings_global.json')
            self.s['scope'] = {'mode': 'selected_regions', 'selected_region_ids': ['x'], 'inclusion': 'room'}
            key = 'proj_1'
            path = os.path.join(tmp, 'settings_{0}.json'.format(key))
            config.settings_path = lambda k, _t=tmp: os.path.join(_t, 'settings_{0}.json'.format(k))
            config.save_settings(self.s, key)
            self.assertEqual(config.read_json(config.GLOBAL_FILE)['scope']['mode'], 'all')
            self.assertEqual(config.read_json(path)['scope']['selected_region_ids'], ['x'])
        finally:
            config.APP_DIR, config.GLOBAL_FILE = old_app, old_global
            import importlib
            importlib.reload(config)
            shutil.rmtree(tmp)


class TestGeometry(unittest.TestCase):
    def test_polygon_with_hole(self):
        outer = [(0, 0), (10, 0), (10, 10), (0, 10)]
        hole = [(4, 4), (6, 4), (6, 6), (4, 6)]
        r = Region('Z', [outer, hole])
        self.assertTrue(r.contains(1, 1))
        self.assertFalse(r.contains(5, 5))
        self.assertFalse(r.contains(11, 5))

    def test_smallest_nested_region_wins_and_level_filter(self):
        big = Region('BIG', [[(0, 0), (100, 0), (100, 100), (0, 100)]], level_id=1)
        small = Region('SMALL', [[(10, 10), (20, 10), (20, 20), (10, 20)]], level_id=1)
        idx = RegionIndex([big, small])
        self.assertEqual(idx.locate(15, 15, 1), 'SMALL')
        self.assertEqual(idx.locate(50, 50, 1), 'BIG')
        self.assertIsNone(idx.locate(50, 50, 2))

    def test_point_in_triangle(self):
        self.assertTrue(point_in_polygon(1, 1, [(0, 0), (4, 0), (0, 4)]))


class TestConfigAndReports(Base):
    def test_defaults_valid(self):
        self.assertEqual(config.validate(self.s), [])

    def test_validation_catches_errors(self):
        self.s['classification_map'].append({'classification': 'lighting', 'panel_type': 'LP', 'rule': 'Nope'})
        self.s['circuit_rules']['Bad'] = {'max_devices': 0, 'max_load_va': 0}
        errs = config.validate(self.s)
        self.assertTrue(any('more than once' in e for e in errs))
        self.assertTrue(any('does not exist' in e for e in errs))
        self.assertTrue(any('neither' in e for e in errs))

    def test_deep_merge(self):
        merged = config.deep_merge({'a': {'x': 1, 'y': 2}}, {'a': {'y': 3}})
        self.assertEqual(merged, {'a': {'x': 1, 'y': 3}})

    def test_exports(self):
        plan = self.run_plan([fx(x=i) for i in range(14)] + [fx('Unicorn')])
        plan.run_id = 'T1'
        sheets = reporting.build_sheets(plan)
        tmp = tempfile.mkdtemp()
        try:
            csvs = reporting.export_csv(sheets, tmp, 'r')
            self.assertEqual(len(csvs), 6)
            path = reporting.export_xlsx(sheets, os.path.join(tmp, 'r.xlsx'))
            with zipfile.ZipFile(path) as z:
                self.assertIn('xl/worksheets/sheet6.xml', z.namelist())
            summary = dict((r[0], r[1]) for r in sheets[0][2])
            self.assertEqual(summary['Uncircuited devices'], 1)
        finally:
            shutil.rmtree(tmp)


if __name__ == '__main__':
    unittest.main()
