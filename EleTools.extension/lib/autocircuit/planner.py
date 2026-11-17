# -*- coding: utf-8 -*-
"""
Circuit planning engine (pure Python - no Revit API).

Pipeline
--------
1. classify   : load classification -> (panel type, rule); decide dedicated circuits
2. bucket     : shared devices grouped by
                (region, level, panel type, rule, poles, voltage, room)
                -> incompatible loads / regions / voltages can never share a circuit
3. pack       : each room bucket ordered spatially (nearest-neighbour chain) and cut
                sequentially at the device-count or VA limit
4. merge      : circuits of rooms with fewer than N devices are merged into an
                adjacent room's circuit (shared wall), else the nearest one within
                the max merge distance - only inside the same partition and only if
                the result still respects the rule limits
5. panels     : PanelSelector assigns a panel to every circuit
"""
from __future__ import division

import fnmatch

from autocircuit.geometry import dist2d, nearest_neighbor_order
from autocircuit.models import PlannedCircuit, CircuitPlan
from autocircuit.panel_selector import PanelSelector

FT_PER_M = 1.0 / 0.3048
NO_ROOM_KEY = '__noroom__'


class ClassificationMapper(object):
    """Maps a Revit load classification name to (panel type, rule). Case-insensitive,
    exact matches first, then wildcard patterns (e.g. 'Lighting*')."""

    def __init__(self, entries):
        self.entries = []
        for e in entries or []:
            cls = (e.get('classification') or '').strip().lower()
            if cls:   # blank panel type is kept: it means "do not circuit"
                self.entries.append((cls, (e.get('panel_type') or '').strip().upper(), e.get('rule')))

    def resolve(self, classification):
        c = (classification or '').strip().lower()
        for pattern, panel_type, rule in self.entries:
            if pattern == c:
                return panel_type, rule
        for pattern, panel_type, rule in self.entries:
            if any(ch in pattern for ch in '*?[') and fnmatch.fnmatch(c, pattern):
                return panel_type, rule
        return None, None


class CircuitPlanner(object):

    def __init__(self, settings, logger=None):
        self.settings = settings
        self.g = settings['general']
        self.rules = settings['circuit_rules']
        self.mapper = ClassificationMapper(settings.get('classification_map', []))
        self.log = logger
        self.unassigned = self.g.get('unassigned_region_name', '<No Region>')

    # ------------------------------------------------------------------ public
    def plan(self, fixtures, panels, rooms):
        plan = CircuitPlan()
        plan.total_fixtures = len(fixtures)
        plan.panels = panels

        eligible = self._classify(fixtures, plan)

        circuits, buckets = [], {}
        for f in eligible:
            if f.dedicated:
                circuits.append(self._circuit([f], dedicated=True))
            else:
                buckets.setdefault(self._bucket_key(f), []).append(f)

        shared = []
        for key in sorted(buckets, key=str):   # deterministic output
            items = buckets[key]
            ordered = nearest_neighbor_order(items, lambda f: f.point)
            shared.extend(self._pack(ordered, self.rules[items[0].rule_name]))

        if self.g.get('allow_room_merging', True):
            shared = self._merge(shared, rooms or {}, plan)
        circuits.extend(shared)

        PanelSelector(panels, self.settings, self.log).assign(circuits, plan)
        plan.circuits = sorted(circuits, key=lambda c: (c.panel.name if c.panel else '~', c.uid))
        return plan

    # -------------------------------------------------------------- classify
    def _classify(self, fixtures, plan):
        out, zero_load = [], 0
        for f in fixtures:
            if f.already_circuited:
                plan.skip(f, 'Already connected to a circuit')
                continue
            if f.locked:
                plan.skip(f, 'Owned by another user (worksharing)')
                continue
            if not f.classification:
                plan.skip(f, 'No load classification found')
                continue
            panel_type, rule_name = self.mapper.resolve(f.classification)
            if panel_type is None:
                plan.skip(f, 'Load classification "{0}" is not in the mapping table'.format(f.classification))
                continue
            if not panel_type:
                plan.skip(f, 'Load classification "{0}" has no panel type (excluded)'.format(f.classification))
                continue
            rule = self.rules.get(rule_name)
            if rule is None:
                plan.skip(f, 'Circuit rule "{0}" is not defined'.format(rule_name))
                continue

            f.panel_type, f.rule_name = panel_type, rule_name
            f.region = f.region or self.unassigned
            max_va = float(rule.get('max_load_va') or 0)
            above = float(rule.get('dedicated_above_va') or 0)

            if rule.get('dedicated'):
                f.dedicated = True
            elif above > 0 and f.load_va > above:
                f.dedicated = True
                f.note = 'Dedicated: {0:.0f} VA > {1:.0f} VA threshold'.format(f.load_va, above)
            elif max_va > 0 and f.load_va > max_va:
                f.dedicated = True
                f.note = 'Dedicated: exceeds circuit limit'
                plan.warn('Element {0} ({1:.0f} VA) exceeds the {2:.0f} VA limit of rule "{3}" - '
                          'placed on its own circuit; check breaker sizing.'.format(
                              f.element_id, f.load_va, max_va, rule_name))
            if f.load_va <= 0:
                zero_load += 1
            out.append(f)
        if zero_load:
            plan.warn('{0} device(s) have zero/unknown load - they are limited by device count only.'.format(zero_load))
        return out

    # ------------------------------------------------------------ keys / pack
    @staticmethod
    def _partition_key(f):
        return (f.region, f.level_id, f.panel_type, f.rule_name, f.poles, int(round(f.voltage or 0)))

    def _bucket_key(self, f):
        return self._partition_key(f) + (f.room_key or NO_ROOM_KEY,)

    @staticmethod
    def _circuit(fixtures, dedicated=False):
        p = fixtures[0]
        return PlannedCircuit(fixtures, p.panel_type, p.rule_name, p.region, p.level_id,
                              p.level_name, dedicated)

    @staticmethod
    def _limits(rule):
        return int(rule.get('max_devices') or 0), float(rule.get('max_load_va') or 0)

    def _pack(self, ordered, rule):
        """Sequential cut keeps each circuit spatially compact (vs. first-fit-decreasing,
        which minimises circuit count but scatters devices across the room)."""
        max_n, max_va = self._limits(rule)
        out, cur, cur_va = [], [], 0.0
        for f in ordered:
            full = (max_n > 0 and len(cur) >= max_n) or (max_va > 0 and cur and cur_va + f.load_va > max_va)
            if cur and full:
                out.append(self._circuit(cur))
                cur, cur_va = [], 0.0
            cur.append(f)
            cur_va += f.load_va
        if cur:
            out.append(self._circuit(cur))
        return out

    # ------------------------------------------------------------------ merge
    def _merge(self, circuits, rooms, plan):
        min_n = int(self.g.get('min_fixtures_before_merge', 0) or 0)
        if min_n <= 1 or not circuits:
            return circuits
        max_d = float(self.g.get('max_merge_distance_m', 0) or 0) * FT_PER_M
        tails = bool(self.g.get('merge_partial_tails', False))

        room_count = {}
        for c in circuits:
            for f in c.fixtures:
                k = self._bucket_key(f)
                room_count[k] = room_count.get(k, 0) + 1

        parts = {}
        for c in circuits:
            parts.setdefault(self._partition_key(c.fixtures[0]), []).append(c)

        result = []
        for pkey in sorted(parts, key=str):
            group = parts[pkey]
            rule = self.rules[group[0].rule_name]
            changed = True
            while changed:   # terminates: each merge removes one circuit
                changed = False
                smalls = sorted([c for c in group if self._eligible(c, min_n, tails, room_count)],
                                key=lambda c: (c.count, c.uid))
                for small in smalls:
                    target = self._best_target(small, group, rule, rooms, max_d)
                    if target is None:
                        continue
                    plan.merges.append({
                        'into': ', '.join(target.room_names), 'merged': ', '.join(small.room_names),
                        'level': small.level_name, 'region': small.region, 'rule': small.rule_name,
                        'devices': small.count})
                    target.absorb(small)
                    group.remove(small)
                    changed = True
                    break
            result.extend(group)
        return result

    def _eligible(self, c, min_n, tails, room_count):
        if c.dedicated or c.count >= min_n:
            return False
        return tails or all(room_count[self._bucket_key(f)] < min_n for f in c.fixtures)

    def _fits(self, target, small, rule):
        max_n, max_va = self._limits(rule)
        if max_n > 0 and target.count + small.count > max_n:
            return False
        if max_va > 0 and target.load_va + small.load_va > max_va:
            return False
        return True

    @staticmethod
    def _adjacent(small, target, rooms):
        s_keys, t_keys = set(small.room_keys), set(target.room_keys)
        if s_keys & t_keys - set([None]):
            return True
        for k in s_keys:
            node = rooms.get(k)
            if node is not None and node.neighbors & t_keys:
                return True
        return False

    def _best_target(self, small, group, rule, rooms, max_d):
        best, best_rank = None, None
        for t in group:
            if t is small or t.dedicated or not self._fits(t, small, rule):
                continue
            adjacent = self._adjacent(small, t, rooms)
            d = dist2d(small.centroid, t.centroid)
            if not adjacent and max_d > 0 and d > max_d:
                continue
            rank = (0 if adjacent else 1, d, t.uid)
            if best_rank is None or rank < best_rank:
                best, best_rank = t, rank
        return best
