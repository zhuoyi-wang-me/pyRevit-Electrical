# -*- coding: utf-8 -*-
"""
Plain data models shared by collectors, planner, builder and reporter.

Deliberately free of Revit API imports so the planning algorithm can be
unit-tested outside Revit. Coordinates are Revit internal units (decimal feet),
loads are VA, element ids are python ints.
"""
from __future__ import division

STATUS_PLANNED = 'Planned'
STATUS_CREATED = 'Created'
STATUS_NO_PANEL = 'No Panel'
STATUS_NO_CAPACITY = 'No Capacity'
STATUS_FAILED = 'Failed'

NO_ROOM = '<No Room>'


class FixtureInfo(object):
    """Snapshot of one circuitable element (fixture, device or equipment)."""

    def __init__(self, element_id, name='', category='', classification='',
                 load_va=0.0, poles=1, voltage=0.0, point=(0.0, 0.0, 0.0),
                 level_id=None, level_name='', room_key=None, room_name='',
                 region=None, already_circuited=False, locked=False,
                 classification_source=''):
        self.element_id = element_id
        self.name = name
        self.category = category
        self.classification = classification
        self.classification_source = classification_source
        self.load_va = float(load_va or 0.0)
        self.poles = int(poles or 1)
        self.voltage = float(voltage or 0.0)
        self.point = point
        self.level_id = level_id
        self.level_name = level_name
        self.room_key = room_key
        self.room_name = room_name or NO_ROOM
        self.region = region
        self.already_circuited = already_circuited
        self.locked = locked
        # --- filled in by the planner
        self.panel_type = None
        self.rule_name = None
        self.dedicated = False
        self.note = ''

    def __repr__(self):
        return '<Fixture {0} {1} {2:.0f}VA {3}>'.format(
            self.element_id, self.classification, self.load_va, self.room_name)


class RoomNode(object):
    """A room / space used for grouping and adjacency."""

    def __init__(self, key, name, level_id=None, centroid=None, boundary_keys=None):
        self.key = key
        self.name = name
        self.level_id = level_id
        self.centroid = centroid
        self.boundary_keys = set(boundary_keys or [])
        self.neighbors = set()


def link_adjacency(nodes):
    """Mark rooms sharing a bounding element (wall / separation line) as neighbours."""
    owners = {}
    for node in nodes.values():
        for bkey in node.boundary_keys:
            owners.setdefault(bkey, set()).add(node.key)
    for keys in owners.values():
        if len(keys) < 2:
            continue
        for k in keys:
            nodes[k].neighbors.update(keys - set([k]))


class PanelInfo(object):
    """Panelboard snapshot plus capacity bookkeeping during planning."""

    def __init__(self, element_id, name, panel_type, level_id=None, level_name='',
                 point=None, max_poles=0, used_poles=0, regions=None, locked=False):
        self.element_id = element_id
        self.name = name
        self.panel_type = (panel_type or '').upper() or None
        self.level_id = level_id
        self.level_name = level_name
        self.point = point
        self.max_poles = int(max_poles or 0)
        self.used_poles = int(used_poles or 0)
        self.regions = set(regions or [])
        self.locked = locked
        self.planned_poles = 0
        self.planned_load_va = 0.0
        self.planned_circuits = 0

    @property
    def free_poles(self):
        return self.max_poles - self.used_poles - self.planned_poles

    @property
    def utilization(self):
        if self.max_poles <= 0:
            return 1.0
        return (self.used_poles + self.planned_poles) / float(self.max_poles)

    def utilization_after(self, poles):
        if self.max_poles <= 0:
            return 1.0
        return (self.used_poles + self.planned_poles + poles) / float(self.max_poles)

    def reserve(self, poles, load_va):
        self.planned_poles += poles
        self.planned_load_va += load_va
        self.planned_circuits += 1

    def release(self, poles, load_va):
        self.planned_poles -= poles
        self.planned_load_va -= load_va
        self.planned_circuits -= 1

    def __repr__(self):
        return '<Panel {0} {1} {2}/{3}>'.format(self.name, self.panel_type,
                                                 self.used_poles + self.planned_poles, self.max_poles)


class PlannedCircuit(object):
    """A group of fixtures intended to become one ElectricalSystem."""
    _seq = [0]

    def __init__(self, fixtures, panel_type, rule_name, region, level_id,
                 level_name='', dedicated=False):
        PlannedCircuit._seq[0] += 1
        self.uid = PlannedCircuit._seq[0]
        self.fixtures = list(fixtures)
        self.panel_type = panel_type
        self.rule_name = rule_name
        self.region = region
        self.level_id = level_id
        self.level_name = level_name
        self.dedicated = dedicated
        self.panel = None
        self.alternates = []          # other acceptable panels, best first
        self.status = STATUS_PLANNED
        self.message = ''
        self.merged_rooms = []        # room names absorbed by room-merging
        self.circuit_number = ''
        self.system_id = None

    @classmethod
    def reset_sequence(cls):
        cls._seq[0] = 0

    @property
    def count(self):
        return len(self.fixtures)

    @property
    def load_va(self):
        return sum(f.load_va for f in self.fixtures)

    @property
    def poles(self):
        return max([f.poles for f in self.fixtures] or [1])

    @property
    def centroid(self):
        if not self.fixtures:
            return None
        n = float(len(self.fixtures))
        return (sum(f.point[0] for f in self.fixtures) / n,
                sum(f.point[1] for f in self.fixtures) / n,
                sum(f.point[2] for f in self.fixtures) / n)

    def _unique(self, attr):
        seen, out = set(), []
        for f in self.fixtures:
            v = getattr(f, attr)
            if v not in seen:
                seen.add(v)
                out.append(v)
        return out

    @property
    def room_keys(self):
        return self._unique('room_key')

    @property
    def room_names(self):
        return self._unique('room_name')

    def absorb(self, other):
        for name in other.room_names:
            if name not in self.merged_rooms and name not in self.room_names:
                self.merged_rooms.append(name)
        self.fixtures.extend(other.fixtures)

    def fail(self, status, message):
        self.status = status
        self.message = message

    def __repr__(self):
        return '<Circuit #{0} {1} {2} n={3} {4:.0f}VA {5}>'.format(
            self.uid, self.panel_type, self.rule_name, self.count, self.load_va, self.status)


class CircuitPlan(object):
    """Result of planning (and, after execution, of creation)."""

    def __init__(self):
        self.circuits = []
        self.skipped = []      # (FixtureInfo, reason)
        self.warnings = []
        self.errors = []
        self.merges = []       # dicts: into, merged, level, region, rule
        self.panels = []
        self.total_fixtures = 0
        self.mode = 'preview'  # or 'execute'
        self.run_id = ''
        self.started = ''
        self.duration_s = 0.0
        self.info = {}

    def skip(self, fixture, reason):
        self.skipped.append((fixture, reason))

    def warn(self, message):
        self.warnings.append(message)

    def error(self, message):
        self.errors.append(message)

    def success_status(self):
        return STATUS_CREATED if self.mode == 'execute' else STATUS_PLANNED

    def successful(self):
        ok = self.success_status()
        return [c for c in self.circuits if c.status == ok]
