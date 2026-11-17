# -*- coding: utf-8 -*-
"""Unit tests for lib/eleccalc/calc.py - run: py -m unittest discover -s tests/ElectricalCalculator"""
import os
import re
import sys
import unittest
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..', '..'))
sys.path.insert(0, os.path.join(ROOT, 'lib'))

from eleccalc import calc  # noqa: E402


class TestLoadCurrent(unittest.TestCase):
    def test_three_phase_kw(self):
        r = calc.load_current(3, 400, 10, 'kW', 0.85)
        self.assertAlmostEqual(r['amps'], 10000 / (3 ** 0.5 * 400 * 0.85), places=3)   # 16.98 A
        self.assertAlmostEqual(r['kva'], 11.765, places=3)

    def test_single_phase_kva(self):
        r = calc.load_current(1, 230, 2.3, 'kVA', 0.9)
        self.assertAlmostEqual(r['amps'], 10.0)
        self.assertAlmostEqual(r['kw'], 2.07)

    def test_amps_round_trip(self):
        r = calc.load_current(3, 400, 100, 'A', 0.8)
        self.assertAlmostEqual(r['amps'], 100.0)
        self.assertAlmostEqual(r['kw'], 3 ** 0.5 * 400 * 100 * 0.8 / 1000)
        self.assertAlmostEqual(r['kvar'], r['kva'] * 0.6)

    def test_bad_input(self):
        for args in ((3, 400, 10, 'kW', 1.2), (3, 0, 10, 'kW', 0.9), (2, 400, 10, 'kW', 0.9),
                     (3, 400, -1, 'kW', 0.9), (3, 400, 1, 'hp', 0.9)):
            self.assertRaises(calc.CalcError, calc.load_current, *args)


class TestVoltDrop(unittest.TestCase):
    def test_bs7671_example(self):
        r = calc.voltage_drop(3, 400, 32, 50, 4.0, 5)        # 4.0 mV/A/m x 32 A x 50 m = 6.4 V
        self.assertAlmostEqual(r['vd_v'], 6.4)
        self.assertAlmostEqual(r['vd_pct'], 1.6)
        self.assertTrue(r['ok'])
        self.assertAlmostEqual(r['max_length_m'], 156.25)

    def test_fails_limit(self):
        r = calc.voltage_drop(1, 230, 20, 100, 7.3, 3)
        self.assertFalse(r['ok'])

    def test_no_limit(self):
        self.assertIsNone(calc.voltage_drop(1, 230, 10, 10, 18, None)['ok'])


class TestCableSizing(unittest.TestCase):
    def test_sized_by_current(self):
        r = calc.size_cable(3, 400, 45, 10, 'XLPE')
        self.assertEqual(r['in_a'], 50)
        self.assertEqual(r['cable']['size_mm2'], 6)           # 4E4A 6 mm2 3-ph It 53 A >= 50 A
        self.assertEqual(r['governed_by'], 'current')

    def test_derating_increases_size(self):
        r = calc.size_cable(3, 400, 45, 10, 'XLPE', ca=0.87, cg=0.7)
        self.assertAlmostEqual(r['it_required'], 50 / (0.87 * 0.7))   # 82.1 A
        self.assertEqual(r['cable']['size_mm2'], 16)
        self.assertLessEqual(r['in_a'], r['cable']['iz'])

    def test_sized_by_volt_drop(self):
        r = calc.size_cable(3, 400, 45, 200, 'XLPE', vd_limit_pct=5)
        self.assertEqual(r['size_by_current'], 6)
        self.assertEqual(r['governed_by'], 'voltage drop')
        self.assertLessEqual(r['cable']['vd_pct'], 5)

    def test_pvc_bigger_than_xlpe(self):
        pvc = calc.size_cable(3, 400, 60, 10, 'PVC')['cable']['size_mm2']     # In 63 A
        xlpe = calc.size_cable(3, 400, 60, 10, 'XLPE')['cable']['size_mm2']
        self.assertEqual((xlpe, pvc), (10, 16))

    def test_fixed_breaker(self):
        self.assertEqual(calc.size_cable(1, 230, 18, 20, breaker=32)['in_a'], 32)
        self.assertRaises(calc.CalcError, calc.size_cable, 1, 230, 40, 20, breaker=32)

    def test_too_big(self):
        r = calc.size_cable(3, 400, 700, 10, 'PVC')
        self.assertIsNone(r['cable'])

    def test_tables_monotonic(self):
        for ins in calc.CABLES:
            prev = None
            for size in calc.cable_sizes(ins):
                row = calc.CABLES[ins][size]
                if prev:
                    self.assertGreater(row[0], prev[0])
                    self.assertGreater(row[1], prev[1])
                    self.assertLessEqual(row[2], prev[2])
                    self.assertLessEqual(row[3], prev[3])
                prev = row


class TestPfAndFault(unittest.TestCase):
    def test_pf_correction(self):
        r = calc.pf_correction(250, 0.8, 0.95)
        self.assertAlmostEqual(r['kvar'], 250 * (0.75 - 0.32868), places=1)  # ~105.3 kVAr
        self.assertAlmostEqual(r['kva_before'], 312.5)
        self.assertRaises(calc.CalcError, calc.pf_correction, 250, 0.95, 0.9)

    def test_transformer_fault_infinite(self):
        r = calc.transformer_fault(1500, 400, 6)
        self.assertAlmostEqual(r['ifl_a'], 2165.06, places=1)
        self.assertAlmostEqual(r['isc_ka'], 36.08, places=1)
        self.assertAlmostEqual(r['isc_ka'], r['isc_inf_ka'])

    def test_transformer_fault_with_source(self):
        r = calc.transformer_fault(1500, 400, 6, source_mva=250)
        self.assertLess(r['isc_ka'], r['isc_inf_ka'])
        self.assertAlmostEqual(r['isc_ka'], 2165.06 / (0.06 + 0.006) / 1000, places=2)


def _load(va, volts=230, poles=1, pf=None, cat='Electrical Fixtures'):
    return {'va': va, 'volts': volts, 'poles': poles, 'pf': pf, 'category': cat}


class TestSummariseLoads(unittest.TestCase):
    def test_single_phase_sockets(self):
        r = calc.summarise_loads([_load(180)] * 10, default_pf=0.9)
        self.assertEqual(r['phases'], 1)
        self.assertEqual(r['volts'], 230)
        self.assertAlmostEqual(r['connected_kva'], 1.8)
        self.assertAlmostEqual(r['connected_kw'], 1.62)
        self.assertAlmostEqual(r['ib'], 1800 / 230.0)
        self.assertEqual(r['breaker'], 10)
        self.assertEqual(r['no_pf'], 10)

    def test_three_phase_auto_and_demand(self):
        items = [_load(15000, 400, 3, 0.85, 'Mechanical Equipment'), _load(1000, 230, 1, 1.0)]
        r = calc.summarise_loads(items, demand_pct=80)
        self.assertEqual(r['phases'], 3)
        self.assertEqual(r['volts'], 400)                     # from the 3-pole load, not the 230 V one
        self.assertAlmostEqual(r['demand_kva'], 16 * 0.8)
        self.assertAlmostEqual(r['ib'], 12800 / (3 ** 0.5 * 400))
        self.assertAlmostEqual(r['connected_kw'], 15 * 0.85 + 1.0)
        self.assertTrue(any('balanced' in w for w in r['warnings']))
        self.assertTrue(any('Mixed voltages' in w for w in r['warnings']))

    def test_forced_supply_and_voltage(self):
        r = calc.summarise_loads([_load(3000)] * 6, supply=3, volts=415)
        self.assertEqual((r['phases'], r['volts']), (3, 415))
        self.assertAlmostEqual(r['ib'], 18000 / (3 ** 0.5 * 415))

    def test_three_phase_without_voltage_defaults_400(self):
        r = calc.summarise_loads([_load(3000, volts=None)], supply=3)
        self.assertEqual(r['volts'], 400)

    def test_missing_loads_counted(self):
        r = calc.summarise_loads([_load(100), _load(None), _load(0)])
        self.assertEqual((r['count'], r['with_load'], r['missing']), (3, 1, 2))

    def test_by_category(self):
        r = calc.summarise_loads([_load(100, cat='A'), _load(50, cat='A'), _load(10, cat='B')])
        self.assertEqual(r['by_category'], {'A': (2, 150.0), 'B': (1, 10.0)})

    def test_errors(self):
        self.assertRaises(calc.CalcError, calc.summarise_loads, [])
        self.assertRaises(calc.CalcError, calc.summarise_loads, [_load(None)])
        self.assertRaises(calc.CalcError, calc.summarise_loads, [_load(100)], demand_pct=0)
        self.assertRaises(calc.CalcError, calc.summarise_loads, [_load(100)], default_pf=1.5)


INF = 1e30


class TestApplyDemand(unittest.TestCase):
    def test_constant(self):
        self.assertAlmostEqual(calc.apply_demand([1000, 1000], 'Constant', [(0, INF, 0.8)]), 1600)

    def test_percent_values_accepted(self):
        self.assertAlmostEqual(calc.apply_demand([1000], 'Constant', [(0, INF, 80)]), 800)

    def test_no_values_is_100(self):
        self.assertAlmostEqual(calc.apply_demand([500, 500], 'Constant', []), 1000)

    def test_quantity_table(self):
        vals = [(1, 3, 1.0), (4, 10, 0.75), (11, INF, 0.5)]
        self.assertAlmostEqual(calc.apply_demand([100] * 3, 'QuantityTable', vals), 300)
        self.assertAlmostEqual(calc.apply_demand([100] * 5, 'QuantityTable', vals), 375)
        self.assertAlmostEqual(calc.apply_demand([100] * 12, 'QuantityTable', vals), 600)

    def test_quantity_per_portion_largest_first(self):
        vals = [(1, 1, 1.0), (2, INF, 0.5)]                    # first unit 100 %, others 50 %
        self.assertAlmostEqual(calc.apply_demand([200, 1000, 400], 'QuantityTablePerPortion', vals),
                               1000 + 0.5 * (400 + 200))

    def test_load_table(self):
        vals = [(0, 10000, 1.0), (10000, INF, 0.6)]
        self.assertAlmostEqual(calc.apply_demand([4000, 4000], 'LoadTable', vals), 8000)
        self.assertAlmostEqual(calc.apply_demand([8000, 8000], 'LoadTable', vals), 16000 * 0.6)

    def test_load_table_per_portion(self):
        # NEC-style lighting: first 3 kVA 100 %, 3-120 kVA 35 %, rest 25 %
        vals = [(0, 3000, 1.0), (3000, 120000, 0.35), (120000, INF, 0.25)]
        self.assertAlmostEqual(calc.apply_demand([5000, 5000], 'LoadTablePerPortion', vals),
                               3000 + 7000 * 0.35)

    def test_enum_style_rule_name_and_additional_load(self):
        self.assertAlmostEqual(
            calc.apply_demand([1000], 'ElectricalDemandFactorRule.Constant', [(0, INF, 0.5)], 250), 750)


class TestDemandByClassification(unittest.TestCase):
    DEFS = {'Lighting': {'name': 'Lighting DF', 'rule': 'Constant', 'values': [(0, INF, 0.9)]},
            'Power': {'name': 'Power DF', 'rule': 'LoadTablePerPortion',
                      'values': [(0, 10000, 1.0), (10000, INF, 0.5)]}}

    def _item(self, va, cls, **kw):
        d = _load(va)
        d['classification'] = cls
        d.update(kw)
        return d

    def test_each_classification_uses_its_factor(self):
        items = [self._item(1000, 'Lighting')] * 4 + [self._item(6000, 'Power')] * 3 + \
                [self._item(500, None)] + [self._item(2000, 'HVAC')]
        r = calc.summarise_loads(items, demand_pct=80, default_pf=1.0, demand_defs=self.DEFS)
        groups = dict((g['name'], g) for g in r['by_classification'])
        self.assertAlmostEqual(groups['Lighting']['demand_va'], 3600)            # 4000 x 90 %
        self.assertAlmostEqual(groups['Power']['demand_va'], 10000 + 8000 * 0.5)  # per portion
        self.assertAlmostEqual(groups['HVAC']['demand_va'], 1600)                # no DF -> fallback 80 %
        self.assertAlmostEqual(groups[calc.UNCLASSIFIED]['demand_va'], 400)
        self.assertAlmostEqual(r['demand_kva'], (3600 + 14000 + 1600 + 400) / 1000.0)
        self.assertAlmostEqual(r['demand_kw'], r['demand_kva'])                  # pf 1
        self.assertEqual(r['mode'], 'classification')
        self.assertTrue(any('without a load classification' in w for w in r['warnings']))

    def test_panel_uses_own_demand(self):
        items = [self._item(50000, None, poles=3, volts=400, demand_va=35000)]
        r = calc.summarise_loads(items, demand_defs={})
        self.assertAlmostEqual(r['demand_kva'], 35)
        self.assertEqual(r['by_classification'][0]['name'], calc.PANEL_DEMAND)

    def test_manual_mode_ignores_classifications(self):
        items = [self._item(1000, 'Lighting')] * 2
        r = calc.summarise_loads(items, demand_pct=50, demand_defs=None)
        self.assertAlmostEqual(r['demand_kva'], 1.0)
        self.assertEqual(r['mode'], 'manual')

    def test_demand_kw_scaled_per_group(self):
        items = [self._item(1000, 'Lighting', pf=0.5), self._item(1000, None, pf=1.0)]
        r = calc.summarise_loads(items, demand_pct=100, demand_defs=self.DEFS)
        self.assertAlmostEqual(r['demand_kw'], 0.5 * 0.9 + 1.0)


class TestIsolator(unittest.TestCase):
    def test_single_phase_minimum_20a(self):
        r = calc.isolator_rating(1000, 230, 1)                  # 4.3 A -> min 20 A
        self.assertEqual((r['rating'], r['poles'], r['duty']), (20, 'DP (2P)', 'AC-22A'))

    def test_three_phase_motor_margin(self):
        # 22 kVA 400 V 3-ph: Ib 31.75 A x 125 % = 39.7 A -> 40 A TP AC-23A
        r = calc.isolator_rating(22000, 400, 3, motor=True)
        self.assertAlmostEqual(r['ib'], 22000 / (3 ** 0.5 * 400))
        self.assertEqual((r['rating'], r['poles'], r['duty']), (40, 'TP (3P)', 'AC-23A'))

    def test_same_load_not_motor_is_smaller(self):
        self.assertEqual(calc.isolator_rating(22000, 400, 3, motor=False)['rating'], 32)

    def test_four_pole_and_defaults(self):
        r = calc.isolator_rating(50000, None, 3, four_pole=True)
        self.assertEqual(r['volts'], 400)
        self.assertEqual(r['poles'], 'TP+N (4P)')
        self.assertEqual(r['rating'], 80)                       # 72.2 A

    def test_two_pole_line_to_line(self):
        r = calc.isolator_rating(8000, 400, 2)
        self.assertAlmostEqual(r['ib'], 20.0)
        self.assertEqual((r['rating'], r['phases']), (20, 1))

    def test_exact_rating_not_bumped(self):
        self.assertEqual(calc.isolator_rating(63 * 230, 230, 1)['rating'], 63)

    def test_too_big(self):
        r = calc.isolator_rating(2e6, 400, 3)
        self.assertIsNone(r['rating'])
        self.assertTrue(r['note'])

    def test_errors(self):
        self.assertRaises(calc.CalcError, calc.isolator_rating, 0, 230, 1)
        self.assertRaises(calc.CalcError, calc.isolator_rating, 1000, 230, 4)
        self.assertRaises(calc.CalcError, calc.isolator_rating, 1000, 230, 1, False, 125, 90)

    def test_motor_detection(self):
        self.assertTrue(calc.is_motor_load('Mechanical Equipment'))
        self.assertTrue(calc.is_motor_load('Electrical Fixtures', 'HVAC'))
        self.assertTrue(calc.is_motor_load('Electrical Equipment', None, 'Isolator : AHU-01 Pump'))
        self.assertFalse(calc.is_motor_load('Lighting Fixtures', 'Lighting', 'Downlight : 18W'))


def _epc():
    from eleccalc.transfer import load_module
    path = os.path.join(ROOT, 'Electrical.tab', 'Electrical.panel', '4 Panels.stack',
                        'Equipment Power Check.pushbutton', 'epc_match.py')
    return load_module('epc_match_for_tests', path)


class TestTransfer(unittest.TestCase):
    def setUp(self):
        from eleccalc import transfer
        self.t = transfer
        self.epc = _epc()

    def _it(self, key, tag, pt):
        return self.epc.Item(key, 'item {0}'.format(key), 'host', pt, tag=tag)

    def test_tag_then_nearest(self):
        loads = [self._it(1, 'AHU-01', (0, 0, 0)), self._it(2, '', (100, 0, 0)), self._it(3, 'PUMP-9', (500, 0, 0))]
        fixtures = [self._it(11, 'AHU-01', (50, 50, 0)),    # far away but same tag
                    self._it(12, '', (102, 0, 0)),           # near load 2
                    self._it(13, 'X', (900, 0, 0))]
        m = self.t.match(loads, fixtures, self.epc, max_plan_ft=10, max_height_ft=13)
        self.assertEqual([(f, h) for f, _, h, _ in m[1]], [(11, 'tag')])
        self.assertEqual([(f, h) for f, _, h, _ in m[2]], [(12, 'nearest')])
        self.assertEqual(m[3], [])

    def test_shared_tag_goes_to_nearest_unit(self):
        loads = [self._it(1, 'FCU', (0, 0, 0)), self._it(2, 'FCU', (100, 0, 0))]
        fixtures = [self._it(11, 'FCU', (1, 0, 0)), self._it(12, 'FCU', (99, 0, 0))]
        m = self.t.match(loads, fixtures, self.epc)
        self.assertEqual([f for f, _, _, _ in m[1]], [11])
        self.assertEqual([f for f, _, _, _ in m[2]], [12])

    def test_plan_writes_and_conflict_takes_largest(self):
        matches = {1: [(11, 'iso A', 'tag', '')], 2: [(11, 'iso A', 'nearest', '')],
                   3: [(13, 'iso C', 'self', '')], 4: [(14, 'iso D', 'tag', '')]}
        writes, conflicts = self.t.plan_writes({1: 20, 2: 40, 3: 63, 4: None}, matches)
        self.assertEqual(writes[11]['rating'], 40)
        self.assertEqual(sorted(writes[11]['loads']), [1, 2])
        self.assertEqual(writes[13]['rating'], 63)
        self.assertNotIn(14, writes)                         # load not rated -> nothing written
        self.assertEqual(conflicts, {11: [20, 40]})

    def test_text_value(self):
        self.assertEqual(self.t.text_value(40), '40A')
        self.assertEqual(self.t.text_value(40.0, '{0}A DP'), '40A DP')
        self.assertEqual(self.t.text_value(63, 'bad {1}'), '63A')


class TestXaml(unittest.TestCase):
    """XAML parses and every control the window code uses exists."""

    def test_names_exist(self):
        btn = os.path.join(ROOT, 'Electrical.tab', 'Electrical.panel', 'Electrical Calculator.pushbutton')
        tree = ET.parse(os.path.join(btn, 'ui.xaml'))
        xns = '{http://schemas.microsoft.com/winfx/2006/xaml}Name'
        names = set(e.get(xns) for e in tree.iter() if e.get(xns))
        with open(os.path.join(ROOT, 'lib', 'eleccalc', 'window.py')) as fh:
            src = fh.read()
        used = set(re.findall(r"self\.((?:md|iso|lc|vd|cs|pf|sc|cb|btn|txt)_\w+|tabs)\b", src))
        used |= set(re.findall(r"(?<!\[)'((?:lc|vd|cs|pf|sc)_\w+)'(?!\])", src))   # not r['vd_v']
        used.discard('iso_tsv')                                                    # attribute, not a control
        self.assertFalse(used - names, 'missing in XAML: {0}'.format(sorted(used - names)))


if __name__ == '__main__':
    unittest.main()
