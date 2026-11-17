# -*- coding: utf-8 -*-
"""Update 3-Phase Circuit Length

Routes 3-phase power circuits along the cable tray network (trays + fittings, horizontal runs and
vertical risers), from the panel to the farthest device, and writes that route into the circuit as a
Custom circuit path - the supported Revit 2025 way to change the native circuit Length.

    - ElectricalSystem.Length and the "Length" parameter are READ-ONLY in the API.
    - ElectricalSystem.SetCircuitPath(IList<XYZ>) is the supported method: Revit then computes Length
      from the path and switches Path Mode to Custom. IsCircuitPathValid() is checked first.
      Revit's rules for a path: first node = the panel connector, every segment exactly horizontal or
      exactly vertical, nodes not too close together.
    - Cable trays / fittings are only READ, never changed (host model or a linked model).
    - Lengths are NOT multiplied by the number of phases.

Which circuits: selected circuits / panels / equipment; nothing selected = pick the panels in a list.
Which trays: pick the model(s) holding the cable trays (host and / or links) in a list (remembered).
Log: %APPDATA%\\EleTools\\CircuitLength\\last_run.txt
"""
__title__ = 'Update 3-Phase\nCircuit Length'
__author__ = 'BHEEM'

import heapq
import io
import math
import os
import time
from collections import defaultdict

from pyrevit import revit, DB, script, forms
from Autodesk.Revit.DB.Electrical import ElectricalSystem, ElectricalSystemType, ElectricalCircuitPathMode
from System.Collections.Generic import List


# ============================================================================== settings (edit here)
NODE_TOL_MM = 10.0          # tray / fitting ends closer than this are one joint (connected)
BRIDGE_TOL_MM = 500.0       # an open tray end this close to another tray (in its own direction) is joined
                            # to it - trays butting into the side of a tray without a tee, small gaps
END_JOIN_DZ_MM = 150.0      # two open tray ends within BRIDGE_TOL_MM of each other and within this height
                            # difference are joined whatever their direction (trays ending side by side)
RISER_GAP_MM = 1500.0      # an open riser (vertical tray) end is joined to the tray straight above / below
                            # it up to this distance - risers cut at the slab
USE_ROOM_RISERS = True      # electrical room rule: vertical trays inside the panel's room (rooms read from the
                            # host / ARC links) serve every level - trays in the room join the riser at their height
ROOM_RISER_REACH_MM = 3000.0  # max sideways distance in the room from a tray joint to the riser
PANEL_TOP_TRAYS = True      # panels / equipment: trays running over the top of the panel are a start point too
                            # (main switchboards - cables go up into the trays above the line-up)
PANEL_TOP_MARGIN_MM = 300.0 # ... trays up to this far outside the panel outline (plan) count as "above"
ROOM_ALL_COLUMNS = False   # False (default, follows the cable tray): a tray joint joins only its NEAREST riser.
                            # True: one join to every riser column in the room (shorter, less realistic routes)
ROOM_SEARCH_MM = 600.0     # panel on / near the room boundary: also look this far around it for the room
GAP_SEARCH_MM = 20000.0    # failed route: how far to look for the place where the two tray networks almost meet
CONNECT_TOL_MM = 3000.0     # max HORIZONTAL distance panel / device connector -> cable tray centreline
MAX_DROP_MM = 8000.0        # max VERTICAL distance panel / device connector -> cable tray (drops)
MAX_CANDIDATES = 8          # trays tried near each panel / device (best route wins)
GRID_MM = 5000.0            # spatial index cell size (speed only)
VERTICAL_RATIO = 0.98       # |dZ| / length >= this  -> vertical riser
HORIZONTAL_RATIO = 0.02     # |dZ| / length <= this  -> horizontal tray, in between -> other (sloped)
PATH_MIN_GAP_MM = 20.0      # circuit path nodes closer than this are merged (Revit refuses too close nodes)
INCLUDE_CONNECTIONS = True  # add panel->tray and tray->device drops to the total route
UPDATE_NATIVE_LENGTH = True # False = report only, the model is not touched


# ============================================================================== small vector helpers
def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _dist(a, b):
    d = _sub(a, b)
    return math.sqrt(_dot(d, d))


def _hdist(a, b):
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2)


def _project(p, a, b):
    """Closest point q on segment a-b to p -> (q, t 0..1 along a->b, distance p-q)."""
    ab = _sub(b, a)
    l2 = float(_dot(ab, ab))
    t = 0.0 if l2 <= 1e-12 else max(0.0, min(1.0, _dot(_sub(p, a), ab) / l2))
    q = (a[0] + ab[0] * t, a[1] + ab[1] * t, a[2] + ab[2] * t)
    return q, t, _dist(p, q)


def _clip_plan(a, b, box):
    """Part of segment a-b inside the plan box (Liang-Barsky) -> (t0, t1) or None. A vertical segment
    (riser) is inside when its plan point is."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, a[0] - box[0]), (dx, box[2] - a[0]), (-dy, a[1] - box[1]), (dy, box[3] - a[1])):
        if abs(p) < 1e-12:
            if q < 0:
                return None
            continue
        r = q / p
        if p < 0:
            t0 = max(t0, r)
        else:
            t1 = min(t1, r)
        if t0 > t1:
            return None
    return t0, t1


def classify(p0, p1):
    """'horizontal', 'vertical' (riser) or 'other' (sloped), from the XYZ of the two ends."""
    length = _dist(p0, p1)
    if length <= 1e-9:
        return 'horizontal'
    ratio = abs(p1[2] - p0[2]) / length
    if ratio >= VERTICAL_RATIO:
        return 'vertical'
    if ratio <= HORIZONTAL_RATIO:
        return 'horizontal'
    return 'other'


# ============================================================================== the tray graph (pure Python)
class Edge(object):
    """One tray segment (a cable tray, a leg of a fitting: connector -> fitting centre, or a bridge)."""
    __slots__ = ('index', 'a', 'b', 'p0', 'p1', 'length', 'kind', 'element_id', 'element_type',
                 'level', 'elevation', 'dead')


class TrayGraph(object):
    """Cable tray network: nodes = joints, edges = tray segments. Built once, reused for every circuit."""

    def __init__(self, node_tol, grid):
        self.tol = node_tol
        self.grid = grid
        self.nodes = []                        # node index -> (x, y, z)
        self.adj = defaultdict(list)           # node -> [(other node, edge index)]
        self.edges = []
        self.bridges = 0
        self.end_dz = node_tol * 15.0          # open end <-> open end join: max height difference
        self._node_cells = defaultdict(list)
        self._edge_cells = defaultdict(list)
        self._keys = set()                     # no duplicate segments
        self._component = None

    @staticmethod
    def _cell(p, size):
        return (int(math.floor(p[0] / size)), int(math.floor(p[1] / size)), int(math.floor(p[2] / size)))

    def node(self, p):
        """The joint at p - ends within the tolerance are the same joint (= connected)."""
        c = self._cell(p, self.tol)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    for n in self._node_cells.get((c[0] + dx, c[1] + dy, c[2] + dz), ()):
                        if _dist(self.nodes[n], p) <= self.tol:
                            return n
        n = len(self.nodes)
        self.nodes.append(p)
        self._node_cells[c].append(n)
        return n

    def _link(self, a, b, length, kind, element_id, element_type=u'', level=u''):
        e = Edge()
        e.index, e.a, e.b = len(self.edges), a, b
        e.p0, e.p1 = self.nodes[a], self.nodes[b]
        e.length, e.kind = length, kind
        e.element_id, e.element_type, e.level = element_id, element_type, level
        e.elevation = (e.p0[2] + e.p1[2]) / 2.0
        e.dead = False
        self.edges.append(e)
        self.adj[a].append((b, e.index))
        self.adj[b].append((a, e.index))
        lo = self._cell(tuple(min(e.p0[i], e.p1[i]) for i in range(3)), self.grid)
        hi = self._cell(tuple(max(e.p0[i], e.p1[i]) for i in range(3)), self.grid)
        for x in range(lo[0], hi[0] + 1):
            for y in range(lo[1], hi[1] + 1):
                for z in range(lo[2], hi[2] + 1):
                    self._edge_cells[(x, y, z)].append(e.index)
        self._component = None
        return e

    def add_edge(self, key, p0, p1, element_id, element_type=u'', level=u''):
        if key in self._keys:
            return None
        self._keys.add(key)
        a, b = self.node(p0), self.node(p1)
        if a == b:
            return None                        # zero length (e.g. a union) - nothing to add
        return self._link(a, b, _dist(p0, p1), classify(p0, p1), element_id, element_type, level)

    def _cells_around(self, p, rh, rv):
        c = self._cell(p, self.grid)
        seen = set()
        for x in range(c[0] - rh, c[0] + rh + 1):
            for y in range(c[1] - rh, c[1] + rh + 1):
                for z in range(c[2] - rv, c[2] + rv + 1):
                    for i in self._edge_cells.get((x, y, z), ()):
                        if i not in seen:
                            seen.add(i)
                            e = self.edges[i]
                            if not e.dead:
                                yield e

    def near(self, p, max_dist, limit):
        """Tray segments within max_dist (3D) of p: [(distance, edge, t, point on tray)]."""
        r = int(math.ceil(max_dist / self.grid))
        found = []
        for e in self._cells_around(p, r, r):
            q, t, d = _project(p, e.p0, e.p1)
            if d <= max_dist:
                found.append((d, e.index, t, q))
        found.sort()
        return [(d, self.edges[i], t, q) for d, i, t, q in found[:limit]]

    def near_drop(self, p, max_h, max_v, limit):
        """Trays a cable can drop to from p: horizontal offset <= max_h, vertical <= max_v.
        Cost = horizontal + vertical (a cable runs square, not diagonal). [(cost, edge, t, point on tray)]"""
        found = []
        for e in self._cells_around(p, int(math.ceil(max_h / self.grid)), int(math.ceil(max_v / self.grid))):
            q, t, _d = _project(p, e.p0, e.p1)
            h, v = _hdist(p, q), abs(p[2] - q[2])
            if h <= max_h and v <= max_v:
                found.append((h + v, e.index, t, q))
        found.sort()
        return [(d, self.edges[i], t, q) for d, i, t, q in found[:limit]]

    def above_box(self, box, z_min, z_max, ref, limit):
        """Trays running over a panel: segments crossing the panel's plan box (xmin, ymin, xmax, ymax) between
        z_min and z_max. For each, the point over the panel nearest (plan) to ref = the connector.
        Cost = plan distance from ref + height. [(cost, edge, t, point)]"""
        cx, cy = (box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0
        half = math.sqrt((box[2] - box[0]) ** 2 + (box[3] - box[1]) ** 2) / 2.0
        zc = (z_min + z_max) / 2.0
        found = []
        for e in self._cells_around((cx, cy, zc), int(math.ceil(half / self.grid)) + 1,
                                    int(math.ceil((z_max - z_min) / 2.0 / self.grid)) + 1):
            span = _clip_plan(e.p0, e.p1, box)
            if span is None:
                continue
            # point of the clipped part nearest to the connector (plan)
            _q, t, _d = _project((ref[0], ref[1], 0.0), (e.p0[0], e.p0[1], 0.0), (e.p1[0], e.p1[1], 0.0))
            t = max(span[0], min(span[1], t))
            q = tuple(e.p0[i] + (e.p1[i] - e.p0[i]) * t for i in range(3))
            if not (z_min <= q[2] <= z_max):
                continue
            found.append((_hdist(ref, q) + abs(q[2] - ref[2]), e.index, t, q))
        found.sort()
        return [(d, self.edges[i], t, q) for d, i, t, q in found[:limit]]

    def diverse(self, candidates, limit, per_network=2):
        """Best candidates, at most per_network from each separate tray network - an isolated tray stub next to the
        connector must not crowd out the real tray network (FIX: main switchboard MDB-CC-N-B1-01)."""
        if not candidates:
            return []
        if not hasattr(self, '_comp_size') or self._comp_size_for is not self._component:
            self.component(0)
            size = defaultdict(int)
            for _n, k in self._component.items():
                size[k] += 1
            self._comp_size, self._comp_size_for = size, self._component
        ordered = sorted(candidates, key=lambda c: c[0])
        out, used, seen = [], defaultdict(int), set()

        def take(c):
            key = (c[1].index, round(c[2], 4))
            if key in seen:
                return
            seen.add(key)
            used[self.component(c[1].a)] += 1
            out.append(c)

        # 1) the best option of EVERY tray network - real (large) networks first, loose stubs after
        firsts = {}
        for c in ordered:
            firsts.setdefault(self.component(c[1].a), c)
        for k, c in sorted(firsts.items(), key=lambda kc: (self._comp_size.get(kc[0], 0) < 20, kc[1][0])):
            if len(out) >= limit:
                break
            take(c)
        # 2) then more options by cost, at most per_network from each network
        for c in ordered:
            if len(out) >= limit:
                break
            if used[self.component(c[1].a)] < per_network:
                take(c)
        return out

    def bridge_gaps(self, max_gap, riser_gap=None, min_dot=0.7, riser_dot=0.95):
        """Join each open tray end to the tray it runs into: a tray butting into the side of another tray
        without a tee, a riser stopping under a tray, a small gap between two trays. The join must lie in the
        direction the open tray points (so stacked / parallel trays are not joined). A riser (vertical tray)
        reaches further (riser_gap), straight up / down only - risers cut at the slab. Returns joins made."""
        riser_gap = max(max_gap, riser_gap or max_gap)
        splits = defaultdict(list)             # edge index -> [(t, node)]
        made = set()
        dangling = [n for n in range(len(self.nodes)) if len(self.adj.get(n, ())) == 1]
        for n in dangling:
            p = self.nodes[n]
            own_ei = self.adj[n][0][1]
            own = self.edges[own_ei]
            back = own.p1 if own.a == n else own.p0
            out = _sub(p, back)
            out_len = math.sqrt(_dot(out, out))
            if out_len <= 1e-9:
                continue
            vertical = own.kind == 'vertical'
            for d, e, t, q in self.near(p, riser_gap if vertical else max_gap, 12):
                if e.index == own_ei or e.a == n or e.b == n or e.element_id == own.element_id:
                    continue
                v = _sub(q, p)
                vl = math.sqrt(_dot(v, v))
                if vl > self.tol and _dot(v, out) / (vl * out_len) < (riser_dot if vertical and d > max_gap else min_dot):
                    continue
                target = self.node(q)
                if target == n:
                    continue
                pair = (min(n, target), max(n, target))
                if pair in made:
                    break
                made.add(pair)
                if target not in (e.a, e.b):
                    splits[e.index].append((t, target))
                self._link(n, target, d, classify(p, self.nodes[target]), own.element_id,
                           own.element_type, own.level)
                break
        # pass 2: two OPEN ends close together at (almost) the same height, whatever their direction - trays
        # ending side by side / offset with no fitting (FIX: tray 26323759 (00) and 33021115 (MZ) ended 0.48 m
        # apart side by side and the two networks were not joined)
        still_open = set(m for m in range(len(self.nodes)) if len(self.adj.get(m, ())) == 1)
        cells = defaultdict(list)
        for m in still_open:
            cells[self._cell(self.nodes[m], max_gap)].append(m)
        for n in sorted(still_open):
            if len(self.adj.get(n, ())) != 1:
                continue                       # joined meanwhile
            p = self.nodes[n]
            c = self._cell(p, max_gap)
            own = self.edges[self.adj[n][0][1]]
            best = None
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for dz in (-1, 0, 1):
                        for m in cells.get((c[0] + dx, c[1] + dy, c[2] + dz), ()):
                            if m == n or len(self.adj.get(m, ())) != 1:
                                continue
                            other = self.edges[self.adj[m][0][1]]
                            if other.element_id == own.element_id:
                                continue
                            q = self.nodes[m]
                            d = _dist(p, q)
                            if d <= max_gap and abs(p[2] - q[2]) <= self.end_dz and (best is None or d < best[0]):
                                best = (d, m)
            if best is None:
                continue
            d, m = best
            pair = (min(n, m), max(n, m))
            if pair in made:
                continue
            made.add(pair)
            self._link(n, m, d, classify(p, self.nodes[m]), own.element_id, own.element_type, own.level)
        self._apply_splits(splits)
        self.bridges = len(made)
        return self.bridges

    def _apply_splits(self, splits):
        """Cut tray segments at the join points: edge index -> [(t, node)]."""
        for ei, points in splits.items():
            e = self.edges[ei]
            if e.dead:
                continue
            e.dead = True                      # replaced by its pieces between the joins
            self.adj[e.a] = [x for x in self.adj[e.a] if x[1] != ei]
            self.adj[e.b] = [x for x in self.adj[e.b] if x[1] != ei]
            chain = [(0.0, e.a)] + sorted(points) + [(1.0, e.b)]
            for (t0, n0), (t1, n1) in zip(chain[:-1], chain[1:]):
                if n0 != n1:
                    self._link(n0, n1, (t1 - t0) * e.length, e.kind, e.element_id, e.element_type, e.level)

    def join_room_risers(self, inside, box, max_h):
        """Electrical room rule: inside the room footprint (plan), every vertical tray is the riser for all
        levels - each tray joint in the room whose height lies on a riser is joined to it, sideways at that
        height (the cable leaves the riser at that floor). inside(p) = plan test, box = (xmin, ymin, xmax, ymax).
        Returns (risers found, joins made)."""
        def in_box(p):
            return box[0] <= p[0] <= box[2] and box[1] <= p[1] <= box[3]

        risers = [e for e in self.edges if not e.dead and e.kind == 'vertical' and in_box(e.p0) and inside(e.p0)]
        if not risers:
            return 0, 0
        nodes_in = [n for n in range(len(self.nodes)) if in_box(self.nodes[n]) and inside(self.nodes[n])]
        splits = defaultdict(list)
        made = 0

        def riser_point(e, z):
            z0, z1 = e.p0[2], e.p1[2]
            t = 0.0 if abs(z1 - z0) < 1e-9 else max(0.0, min(1.0, (z - z0) / (z1 - z0)))
            return t, (e.p0[0] + (e.p1[0] - e.p0[0]) * t, e.p0[1] + (e.p1[1] - e.p0[1]) * t, z0 + (z1 - z0) * t)

        # riser LADDERS: the vertical pieces standing on the same spot (plan) are one riser column
        # FIX 1: every joint was joined to every vertical PIECE within reach (12 967 joins in one room)
        # FIX 2: one riser only per joint was too strict (that ladder may not go up to the device's level - routes
        # doubled) - now one join per riser COLUMN, to the piece of that column at the joint's height
        col_size = max(self.tol * 5.0, 1e-6)
        columns = defaultdict(list)
        for e in risers:
            columns[(int(round(e.p0[0] / col_size)), int(round(e.p0[1] / col_size)))].append(e)
        self.room_columns = len(columns)

        def riser_pieces(p, skip_ids):
            """The riser(s) a tray joint joins: the NEAREST one only (default - confirmed by the user to follow the
            cable tray: joining every riser column let routes jump between ladders and come out too short), or
            one per riser column when ROOM_ALL_COLUMNS is on. -> [(h, edge)]"""
            out = []
            for pieces in columns.values():
                for e in pieces:
                    if e.element_id in skip_ids:
                        continue
                    lo, hi = min(e.p0[2], e.p1[2]), max(e.p0[2], e.p1[2])
                    if lo - self.tol <= p[2] <= hi + self.tol:
                        h = _hdist(p, e.p0)
                        if h <= max_h:
                            out.append((h, e))
                        break                  # one piece per column
            if not ROOM_ALL_COLUMNS and out:
                out = [min(out, key=lambda x: x[0])]
            return out

        for n in nodes_in:
            touching = [self.edges[ei] for _v, ei in self.adj.get(n, ())]
            if touching and all(x.kind == 'vertical' for x in touching):
                continue                       # a joint on a riser itself - not a floor tray
            p = self.nodes[n]
            for h, e in riser_pieces(p, set(x.element_id for x in touching)):
                if e.dead:
                    continue
                t, q = riser_point(e, p[2])
                target = self.node(q)
                if target == n or [1 for v, _ei in self.adj.get(n, ()) if v == target]:
                    continue
                if target not in (e.a, e.b):
                    splits[e.index].append((t, target))
                self._link(n, target, h, 'horizontal', e.element_id, e.element_type, e.level)
                made += 1
        # trays running THROUGH the room (no joint inside it): join at the point of the tray nearest its riser
        crossing = [e2 for e2 in self.edges if not e2.dead and e2.kind != 'vertical'
                    and min(e2.p0[0], e2.p1[0]) <= box[2] and max(e2.p0[0], e2.p1[0]) >= box[0]
                    and min(e2.p0[1], e2.p1[1]) <= box[3] and max(e2.p0[1], e2.p1[1]) >= box[1]]
        for e2 in crossing:
            if inside(e2.p0) or inside(e2.p1):
                continue                       # has a joint in the room - joined above
            ranked = []
            for pieces in columns.values():
                axis = (pieces[0].p0[0], pieces[0].p0[1], 0.0)
                r, t2, _d = _project(axis, (e2.p0[0], e2.p0[1], 0.0), (e2.p1[0], e2.p1[1], 0.0))
                ranked.append((_hdist(r, axis), pieces))
            ranked.sort(key=lambda x: x[0])
            # nearest riser only (default: first one that can be joined), or every column when ROOM_ALL_COLUMNS is on
            joined_one = False
            for _h, pieces in ranked:
                if joined_one and not ROOM_ALL_COLUMNS:
                    break
                axis = (pieces[0].p0[0], pieces[0].p0[1], 0.0)
                r, t2, _d = _project(axis, (e2.p0[0], e2.p0[1], 0.0), (e2.p1[0], e2.p1[1], 0.0))
                if t2 <= 0.0 or t2 >= 1.0:
                    continue
                rz = e2.p0[2] + (e2.p1[2] - e2.p0[2]) * t2
                rp = (r[0], r[1], rz)
                piece = None
                for e in pieces:
                    lo, hi = min(e.p0[2], e.p1[2]), max(e.p0[2], e.p1[2])
                    if e.element_id != e2.element_id and lo - self.tol <= rz <= hi + self.tol:
                        piece = e
                        break
                if piece is None or piece.dead:
                    continue
                h = _hdist(rp, piece.p0)
                if h > max_h or not inside(rp):
                    continue
                t, q = riser_point(piece, rz)
                on_riser, on_tray = self.node(q), self.node(rp)
                if on_riser == on_tray:
                    continue
                if on_riser not in (piece.a, piece.b):
                    splits[piece.index].append((t, on_riser))
                if on_tray not in (e2.a, e2.b):
                    splits[e2.index].append((t2, on_tray))
                self._link(on_tray, on_riser, h, 'horizontal', piece.element_id, piece.element_type, piece.level)
                made += 1
                joined_one = True
        self._apply_splits(splits)
        return len(risers), made

    def component(self, n):
        """Connected tray network number of node n (for messages)."""
        if self._component is None:
            comp, k = {}, 0
            for s in range(len(self.nodes)):
                if s in comp:
                    continue
                comp[s] = k
                stack = [s]
                while stack:
                    u = stack.pop()
                    for v, _ei in self.adj.get(u, ()):
                        if v not in comp:
                            comp[v] = k
                            stack.append(v)
                k += 1
            self._component = comp
        return self._component.get(n)

    def closest_gap(self, n1, n2, max_dist):
        """Closest pair of joints between the tray network of n1 and that of n2 (where the networks almost
        meet = where the tray model has its gap). -> (distance, node, node) or None."""
        c1, c2 = self.component(n1), self.component(n2)
        if c1 is None or c2 is None or c1 == c2:
            return None
        if not hasattr(self, '_gap_cache'):
            self._gap_cache = {}
        key = (min(c1, c2), max(c1, c2), max_dist)
        if key in self._gap_cache:
            return self._gap_cache[key]
        a_nodes = [n for n, c in self._component.items() if c == c1]
        b_nodes = [n for n, c in self._component.items() if c == c2]
        if len(a_nodes) > len(b_nodes):
            a_nodes, b_nodes = b_nodes, a_nodes
        cells = defaultdict(list)
        for n in b_nodes:
            cells[self._cell(self.nodes[n], max_dist)].append(n)
        best = None
        for n in a_nodes:
            p = self.nodes[n]
            c = self._cell(p, max_dist)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for dz in (-1, 0, 1):
                        for m in cells.get((c[0] + dx, c[1] + dy, c[2] + dz), ()):
                            d = _dist(p, self.nodes[m])
                            if d <= max_dist and (best is None or d < best[0]):
                                best = (d, n, m)
        self._gap_cache[key] = best
        return best

    def edge_at(self, n):
        """A tray segment ending at joint n."""
        for _v, ei in self.adj.get(n, ()):
            return self.edges[ei]
        return None

    def route(self, start, start_near, end, end_near):
        """Shortest connected route (Dijkstra) start -> tray ... tray -> end.
        start_near / end_near = near_drop() results. Returns [step] or None, step = (kind, length,
        element_id, p_from, p_to). Kind 'connection' = panel->tray / tray->device drop."""
        S, T = ('S',), ('T',)
        extra = defaultdict(list)
        for i, (d, e, t, q) in enumerate(start_near):
            s = ('s', i)
            extra[S].append((s, d, ('connection', d, None, start, q)))
            extra[s].append((e.a, t * e.length, (e.kind, t * e.length, e.element_id, q, e.p0)))
            extra[s].append((e.b, (1 - t) * e.length, (e.kind, (1 - t) * e.length, e.element_id, q, e.p1)))
            for j, (d2, e2, t2, q2) in enumerate(end_near):
                if e2.index == e.index:        # both on the same tray: straight along it
                    w = abs(t2 - t) * e.length
                    extra[s].append((('t', j), w, (e.kind, w, e.element_id, q, q2)))
        for j, (d, e, t, q) in enumerate(end_near):
            g = ('t', j)
            extra[e.a].append((g, t * e.length, (e.kind, t * e.length, e.element_id, e.p0, q)))
            extra[e.b].append((g, (1 - t) * e.length, (e.kind, (1 - t) * e.length, e.element_id, e.p1, q)))
            extra[g].append((T, d, ('connection', d, None, q, end)))

        best = {S: 0.0}
        prev = {}
        heap = [(0.0, 0, S)]
        counter = 1
        done = set()
        while heap:
            cost, _n, u = heapq.heappop(heap)
            if u in done:
                continue
            done.add(u)                        # each joint once -> no loops, no double counting
            if u == T:
                break
            moves = list(extra.get(u, ()))
            if isinstance(u, int):
                for v, ei in self.adj.get(u, ()):
                    e = self.edges[ei]
                    moves.append((v, e.length, (e.kind, e.length, e.element_id, self.nodes[u], self.nodes[v])))
            for v, w, step in moves:
                if v in done:
                    continue
                nc = cost + w
                if nc < best.get(v, float('inf')) - 1e-9:
                    best[v] = nc
                    prev[v] = (u, step)
                    heapq.heappush(heap, (nc, counter, v))
                    counter += 1
        if T not in prev:
            return None
        steps, u = [], T
        while u != S:
            u, step = prev[u]
            steps.append(step)
        steps.reverse()
        return steps


def totals(steps):
    out = defaultdict(float)
    for kind, length, _eid, _a, _b in steps:
        out[kind] += length
    return out


def path_points(steps):
    """Route polyline (panel ... device), as the points the steps pass through."""
    pts = []
    for _k, _l, _e, a, b in steps:
        for p in (a, b):
            if not pts or _dist(pts[-1], p) > 1e-6:
                pts.append(p)
    return pts


def orthogonal(points, min_gap):
    """Revit's circuit path rule: every segment exactly horizontal or exactly vertical, nodes not too close.
    Sloped pieces get a corner (the horizontal run at the higher level - tray level), small skews are
    snapped, near-duplicate points dropped, straight runs merged. The first point is kept exactly."""
    if not points:
        return []
    out = [points[0]]
    for b in points[1:]:
        a = out[-1]
        h, v = _hdist(a, b), abs(a[2] - b[2])
        if h < min_gap and v < min_gap:
            continue
        if h < min_gap:
            out.append((a[0], a[1], b[2]))                 # vertical
        elif v < min_gap:
            out.append((b[0], b[1], a[2]))                 # horizontal
        else:
            corner = (b[0], b[1], a[2]) if a[2] >= b[2] else (a[0], a[1], b[2])
            out.append(corner)
            out.append(b)
    # merge straight runs (a middle point on the same line, same direction)
    merged = [out[0]]
    for i in range(1, len(out)):
        p = out[i]
        if len(merged) >= 2 and i < len(out):
            a, b = merged[-2], merged[-1]
            d1, d2 = _sub(b, a), _sub(p, b)
            l1, l2 = math.sqrt(_dot(d1, d1)), math.sqrt(_dot(d2, d2))
            if l1 > 0 and l2 > 0 and _dot(d1, d2) / (l1 * l2) > 0.999999:
                merged[-1] = p
                continue
        merged.append(p)
    return merged


def path_length(points):
    return sum(_dist(a, b) for a, b in zip(points[:-1], points[1:]))


# ============================================================================== Revit side
def mm(v):
    return DB.UnitUtils.ConvertToInternalUnits(v, DB.UnitTypeId.Millimeters)


def meters(v):
    return DB.UnitUtils.ConvertFromInternalUnits(v, DB.UnitTypeId.Meters)


def _xyz(p):
    return (p.X, p.Y, p.Z)


def _eid(element_id):
    try:
        return element_id.Value                # Revit 2024+
    except AttributeError:
        return element_id.IntegerValue


def _connectors(element):
    try:
        if isinstance(element, DB.MEPCurve):
            return list(element.ConnectorManager.Connectors)
        model = getattr(element, 'MEPModel', None)
        if model is not None and model.ConnectorManager is not None:
            return list(model.ConnectorManager.Connectors)
    except Exception:
        pass
    return []


def _tray_count(doc):
    return (DB.FilteredElementCollector(doc).OfCategory(DB.BuiltInCategory.OST_CableTray)
            .WhereElementIsNotElementType().GetElementCount())


def _add_trays(graph, doc, transform, source):
    """Add the cable trays + fittings of one document (host or link) to the graph, in HOST coordinates."""
    type_names, level_names = {}, {}

    def pt(p):
        return _xyz(transform.OfPoint(p) if transform is not None else p)

    def type_name(el):
        tid = el.GetTypeId()
        key = _eid(tid)
        if key not in type_names:
            t = doc.GetElement(tid)
            type_names[key] = DB.Element.Name.GetValue(t) if t is not None else u''
        return type_names[key]

    def level_name(level_id):
        if level_id is None or level_id == DB.ElementId.InvalidElementId:
            return u''
        key = _eid(level_id)
        if key not in level_names:
            lvl = doc.GetElement(level_id)
            level_names[key] = lvl.Name if lvl is not None else u''
        return level_names[key]

    trays = (DB.FilteredElementCollector(doc).OfCategory(DB.BuiltInCategory.OST_CableTray)
             .WhereElementIsNotElementType().ToElements())
    for tray in trays:
        loc = tray.Location
        if not isinstance(loc, DB.LocationCurve):
            continue
        curve = loc.Curve
        try:
            lvl = tray.ReferenceLevel.Id if tray.ReferenceLevel is not None else None
        except Exception:
            lvl = None
        key = (source, _eid(tray.Id))
        graph.add_edge(('tray',) + key, pt(curve.GetEndPoint(0)), pt(curve.GetEndPoint(1)),
                       key, u'Cable Tray: ' + type_name(tray), level_name(lvl))

    fittings = (DB.FilteredElementCollector(doc).OfCategory(DB.BuiltInCategory.OST_CableTrayFitting)
                .WhereElementIsNotElementType().ToElements())
    for fit in fittings:
        loc = fit.Location
        if isinstance(loc, DB.LocationPoint):
            centre = pt(loc.Point)
        else:
            box = fit.get_BoundingBox(None)
            if box is None:
                continue
            centre = pt((box.Min + box.Max) * 0.5)
        key = (source, _eid(fit.Id))
        # one leg per connector: connector -> fitting centre (each fitting counted once, legs never twice)
        for con in _connectors(fit):
            graph.add_edge(('fit',) + key + (con.Id,), pt(con.Origin), centre, key,
                           u'Cable Tray Fitting: ' + type_name(fit), level_name(fit.LevelId))
    return len(trays), len(fittings)


class TraySource(object):
    """Host model or one loaded link that holds cable trays."""

    def __init__(self, key, name, doc, transform, trays):
        self.key, self.name, self.doc, self.transform, self.trays = key, name, doc, transform, trays
        self.title = doc.Title

    @property
    def label(self):
        return u'%s   (%d cable trays)' % (self.name, self.trays)


def tray_sources(doc):
    """Host + loaded links that contain cable trays. Unloaded links are listed in the log."""
    found, skipped = [], []
    n = _tray_count(doc)
    if n:
        found.append(TraySource(u'host', u'Host model: ' + doc.Title, doc, None, n))
    for link in DB.FilteredElementCollector(doc).OfClass(DB.RevitLinkInstance).ToElements():
        link_doc = link.GetLinkDocument()
        if link_doc is None:
            skipped.append(link.Name + u' (not loaded)')
            continue
        n = _tray_count(link_doc)
        if n:
            found.append(TraySource(u'link%s' % _eid(link.Id), link.Name, link_doc, link.GetTotalTransform(), n))
    return found, skipped


def build_graph(sources):
    """Read every cable tray and fitting (elbows, tees, crosses, transitions, unions ...) of the chosen
    sources once, in host coordinates, then join open tray ends to the tray they run into."""
    graph = TrayGraph(mm(NODE_TOL_MM), mm(GRID_MM))
    graph.end_dz = mm(END_JOIN_DZ_MM)
    counts = []
    for s in sources:
        t, f = _add_trays(graph, s.doc, s.transform, s.key)
        counts.append((s, t, f))
    graph.bridge_gaps(mm(BRIDGE_TOL_MM), mm(RISER_GAP_MM))
    return graph, counts


# ---------------------------------------------------------------------------------------- rooms (host / ARC links)
class RoomRef(object):
    """A room of the host or of a linked (ARC) model, with its box in HOST coordinates."""

    def __init__(self, room, to_link, source):
        self.room, self.to_link, self.source = room, to_link, source
        try:
            self.name = u'%s %s' % (room.Number or u'', room.get_Parameter(DB.BuiltInParameter.ROOM_NAME).AsString() or u'')
        except Exception:
            self.name = u'Room %s' % _eid(room.Id)
        bb = room.get_BoundingBox(None)
        self.z_test = bb.Min.Z + mm(300.0)     # plan test height, in the room's own model
        to_host = to_link.Inverse if to_link is not None else None
        pts = []
        for x in (bb.Min.X, bb.Max.X):
            for y in (bb.Min.Y, bb.Max.Y):
                for z in (bb.Min.Z, bb.Max.Z):
                    p = DB.XYZ(x, y, z)
                    pts.append(_xyz(to_host.OfPoint(p) if to_host is not None else p))
        self.box = (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts))
        self.z_lo, self.z_hi = min(p[2] for p in pts), max(p[2] for p in pts)
        self.key = (source, _eid(room.Id))

    def in_plan(self, p):
        """p (host) inside the room outline, at any height."""
        if not (self.box[0] <= p[0] <= self.box[2] and self.box[1] <= p[1] <= self.box[3]):
            return False
        q = DB.XYZ(p[0], p[1], p[2])
        if self.to_link is not None:
            q = self.to_link.OfPoint(q)
        try:
            return self.room.IsPointInRoom(DB.XYZ(q.X, q.Y, self.z_test))
        except Exception:
            return False


def load_rooms(doc):
    """Placed rooms of the host model and of every loaded link (the ARC models)."""
    refs = []
    sources = [(doc, None, u'host')]
    for link in DB.FilteredElementCollector(doc).OfClass(DB.RevitLinkInstance).ToElements():
        link_doc = link.GetLinkDocument()
        if link_doc is not None:
            sources.append((link_doc, link.GetTotalTransform().Inverse, u'link%s' % _eid(link.Id)))
    for d, to_link, source in sources:
        for room in DB.FilteredElementCollector(d).OfCategory(DB.BuiltInCategory.OST_Rooms).WhereElementIsNotElementType():
            try:
                if room.Area <= 0 or room.get_BoundingBox(None) is None:
                    continue
                refs.append(RoomRef(room, to_link, source))
            except Exception:
                pass
    return refs


def room_of(rooms, p):
    """The room the panel stands in (on its own level), else one within ROOM_SEARCH_MM around it."""
    reach = mm(ROOM_SEARCH_MM)
    level_ok = [r for r in rooms if r.z_lo - mm(1000.0) <= p[2] <= r.z_hi + mm(1000.0)]
    offsets = [(0.0, 0.0)] + [(reach * math.cos(a * math.pi / 4.0), reach * math.sin(a * math.pi / 4.0)) for a in range(8)]
    for dx, dy in offsets:
        q = (p[0] + dx, p[1] + dy, p[2])
        hits = [r for r in level_ok if r.in_plan(q)]
        if hits:
            elec = [r for r in hits if u'ELEC' in r.name.upper()]
            return (elec or hits)[0]
    return None


def join_panel_rooms(graph, circuits, rooms):
    """Electrical room rule for the panels of these circuits -> {panel id: (room name, risers, joins) or None}."""
    done, out = {}, {}
    for c in circuits:
        panel = c.BaseEquipment
        if panel is None or _eid(panel.Id) in out:
            continue
        p = circuit_start(c, panel)
        r = room_of(rooms, p) if p is not None else None
        if r is None:
            out[_eid(panel.Id)] = None
            continue
        if r.key not in done:
            graph.room_columns = 0
            risers, joins = graph.join_room_risers(r.in_plan, r.box, mm(ROOM_RISER_REACH_MM))
            done[r.key] = (risers, graph.room_columns, joins)
        out[_eid(panel.Id)] = (r.name.strip(),) + done[r.key]
    return out


def is_three_phase(circuit):
    try:
        return circuit.SystemType == ElectricalSystemType.PowerCircuit and circuit.PolesNumber == 3
    except Exception:
        return False


def _is_panel(element):
    try:
        return element.Category is not None and _eid(element.Category.Id) == int(DB.BuiltInCategory.OST_ElectricalEquipment)
    except Exception:
        return False


def _fed_by(element):
    """Circuits an element feeds (panel) - else the circuits it is on (load)."""
    model = getattr(element, 'MEPModel', None)
    if model is None:
        return []
    if _is_panel(element):
        try:
            fed = list(model.GetAssignedElectricalSystems() or [])
            if fed:
                return fed
        except Exception:
            pass
    try:
        return list(model.GetElectricalSystems() or [])
    except Exception:
        return []


def _panel_label(panel):
    return element_label(panel)


def circuit_start(circuit, panel):
    """First node of the circuit path = the panel connector (Revit's own first path node)."""
    try:
        own = list(circuit.GetCircuitPath())
        if own:
            return _xyz(own[0])
    except Exception:
        pass
    return connector_point(panel, circuit)


def connector_point(element, circuit):
    """The element's electrical connector of this circuit, else any electrical connector, else its location."""
    electrical = []
    for con in _connectors(element):
        try:
            if con.Domain != DB.Domain.DomainElectrical:
                continue
        except Exception:
            continue
        electrical.append(con)
        try:
            if con.MEPSystem is not None and con.MEPSystem.Id == circuit.Id:
                return _xyz(con.Origin)
        except Exception:
            pass
    if electrical:
        return _xyz(electrical[0].Origin)
    loc = element.Location
    if isinstance(loc, DB.LocationPoint):
        return _xyz(loc.Point)
    box = element.get_BoundingBox(None)
    return _xyz((box.Min + box.Max) * 0.5) if box is not None else None


def element_label(element):
    for bip in (DB.BuiltInParameter.RBS_ELEC_PANEL_NAME, DB.BuiltInParameter.ALL_MODEL_MARK):
        try:
            p = element.get_Parameter(bip)
            if p is not None and p.AsString():
                return p.AsString()
        except Exception:
            pass
    try:
        return u'%s : %s' % (element.Symbol.FamilyName, DB.Element.Name.GetValue(element))
    except Exception:
        return u'Element %s' % _eid(element.Id)


class Outcome(object):
    def __init__(self, circuit):
        self.circuit = circuit
        self.panel = u''
        self.destination = u''
        self.devices = 0
        self.steps = None
        self.sums = {}
        self.total = 0.0
        self.points = []
        self.status = u'FAILED'
        self.reason = u''
        self.notes = []
        self.length_before = None
        self.length_after = None
        self.room = u''


def _tray_ids(near_list):
    out = []
    for _d, e, _t, _q in near_list[:3]:
        if e.element_id is not None and e.element_id[1] not in out:
            out.append(e.element_id[1])
    return u', '.join(u'%s' % i for i in out)


def _tray_text(e):
    if e is None or e.element_id is None:
        return u'?'
    return u'Id %s%s' % (e.element_id[1], (u' (%s)' % e.level) if e.level else u'')


def _gap_text(graph, start_near, end_near):
    """Where the panel's tray network and the device's tray network come closest = the gap to fix."""
    try:
        gap = graph.closest_gap(start_near[0][1].a, end_near[0][1].a, mm(GAP_SEARCH_MM))
    except Exception:
        return u''
    if gap is None:
        return u'. The two tray networks never come within %d m of each other - the riser / link tray between ' \
               u'them is missing.' % (GAP_SEARCH_MM / 1000.0)
    d, n1, n2 = gap
    p1, p2 = graph.nodes[n1], graph.nodes[n2]

    def end(n):
        k = len(graph.adj.get(n, ()))
        return u'open end' if k == 1 else u'joint of %d segments' % k

    return (u'. GAP TO FIX: the networks come closest between tray %s [%s] and tray %s [%s] - %.2f m apart '
            u'(%.2f m horizontal, %.2f m vertical).' % (
                _tray_text(graph.edge_at(n1)), end(n1), _tray_text(graph.edge_at(n2)), end(n2), meters(d),
                meters(_hdist(p1, p2)), meters(abs(p1[2] - p2[2]))))


def tray_options(graph, element, point, max_h, max_v):
    """Where the cable can join the tray at a panel / device: trays near its connector, plus - for panels and
    other electrical equipment - trays running over the top of it (main switchboards: cables leave through the
    top into the trays above the whole line-up). Best options from each separate tray network."""
    found = graph.near_drop(point, max_h, max_v, MAX_CANDIDATES * 4)
    if PANEL_TOP_TRAYS and _is_panel(element):
        try:
            bb = element.get_BoundingBox(None)
        except Exception:
            bb = None
        if bb is not None:
            grow = mm(PANEL_TOP_MARGIN_MM)
            box = (bb.Min.X - grow, bb.Min.Y - grow, bb.Max.X + grow, bb.Max.Y + grow)
            found += graph.above_box(box, bb.Max.Z - mm(300.0), bb.Max.Z + max_v, point, MAX_CANDIDATES * 4)
    return graph.diverse(found, MAX_CANDIDATES)


def solve(graph, circuit):
    """Route one circuit: panel -> tray network -> each device; the farthest device sets the length."""
    out = Outcome(circuit)
    max_h, max_v = mm(CONNECT_TOL_MM), mm(MAX_DROP_MM)
    try:
        out.length_before = circuit.Length
    except Exception:
        pass
    panel = circuit.BaseEquipment
    if panel is None:
        out.reason = u'Circuit is not connected to a panel (no source panel).'
        return out
    out.panel = element_label(panel)
    devices = [d for d in circuit.Elements if d is not None and d.Id != panel.Id]
    out.devices = len(devices)
    if not devices:
        out.reason = u'Circuit has no connected equipment / fixtures.'
        return out
    start = circuit_start(circuit, panel)
    if start is None:
        out.reason = u'Panel %s has no connector / location.' % out.panel
        return out
    start_near = tray_options(graph, panel, start, max_h, max_v)
    if not start_near:
        out.reason = u'No cable tray within %d mm (plan) / %d mm (height) of panel %s.' % (
            CONNECT_TOL_MM, MAX_DROP_MM, out.panel)
        return out
    best, failed = None, []
    for dev in devices:
        end = connector_point(dev, circuit)
        name = element_label(dev)
        if end is None:
            failed.append(u'%s (no connector / location)' % name)
            continue
        end_near = tray_options(graph, dev, end, max_h, max_v)
        if not end_near:
            failed.append(u'%s (no cable tray within %d mm plan / %d mm height)' % (name, CONNECT_TOL_MM, MAX_DROP_MM))
            continue
        steps = graph.route(start, start_near, end, end_near)
        if steps is None:
            failed.append(u'%s (tray network at the device is not connected to the one at the panel: trays near '
                          u'panel Id %s, near device Id %s)%s' % (
                              name, _tray_ids(start_near), _tray_ids(end_near), _gap_text(graph, start_near, end_near)))
            continue
        sums = totals(steps)
        total = sum(v for k, v in sums.items() if k != 'connection' or INCLUDE_CONNECTIONS)
        if best is None or total > best[0]:
            best = (total, steps, sums, name)
    if failed:
        out.notes.append(u'Not routed: ' + u'; '.join(failed))
    if best is None:
        out.reason = u'No valid connected cable tray route found.'
        return out
    out.total, out.steps, out.sums, out.destination = best
    if out.devices > 1:
        out.destination += u'  (farthest of %d devices)' % out.devices
    out.points = orthogonal(path_points(out.steps), mm(PATH_MIN_GAP_MM))
    out.status = u'CALCULATED'
    return out


def write_path(doc, out):
    """Write the route as a Custom circuit path (supported API). Returns (ok, message)."""
    circuit = out.circuit
    if not hasattr(circuit, 'SetCircuitPath'):
        return False, u'This Revit version has no ElectricalSystem.SetCircuitPath.'
    tries = [out.points]
    try:
        own = list(circuit.GetCircuitPath())
        if len(own) >= 2:
            # second try: also end exactly on Revit's own last node (the device connector Revit uses)
            tries.append(orthogonal(out.points + [_xyz(own[-1])], mm(PATH_MIN_GAP_MM)))
    except Exception:
        pass
    last = u''
    for pts in tries:
        if len(pts) < 2:
            last = u'Route too short for a circuit path.'
            continue
        nodes = List[DB.XYZ]([DB.XYZ(p[0], p[1], p[2]) for p in pts])
        try:
            if hasattr(circuit, 'IsCircuitPathValid') and not circuit.IsCircuitPathValid(nodes):
                last = u'Revit rejected the route as a circuit path (IsCircuitPathValid = False, %d nodes).' % len(pts)
                continue
            circuit.SetCircuitPath(nodes)      # Path Mode becomes Custom; Revit recomputes Length
            doc.Regenerate()
            try:
                if circuit.CircuitPathMode != ElectricalCircuitPathMode.Custom:
                    circuit.CircuitPathMode = ElectricalCircuitPathMode.Custom
            except Exception:
                pass
            return True, u''
        except Exception as ex:
            last = u'SetCircuitPath failed: %s' % ex
    return False, last


class _QuietWarnings(DB.IFailuresPreprocessor):
    """Drop warnings (not errors) so a long run is not stopped by dialogs."""

    def PreprocessFailures(self, accessor):
        for failure in accessor.GetFailureMessages():
            if failure.GetSeverity() == DB.FailureSeverity.Warning:
                accessor.DeleteWarning(failure)
        return DB.FailureProcessingResult.Continue


def update_model(doc, outcomes):
    routed = [o for o in outcomes if o.status == u'CALCULATED']
    if not routed or not UPDATE_NATIVE_LENGTH:
        return
    if doc.IsReadOnly:
        for o in routed:
            o.notes.append(u'Document is read-only - native length not updated.')
        return
    t = DB.Transaction(doc, 'Update 3-Phase Circuit Length')
    t.Start()
    opts = t.GetFailureHandlingOptions()
    opts.SetFailuresPreprocessor(_QuietWarnings())
    t.SetFailureHandlingOptions(opts)
    try:
        for o in routed:
            sub = DB.SubTransaction(doc)
            sub.Start()
            try:
                ok, message = write_path(doc, o)
            except Exception as ex:
                ok, message = False, u'%s' % ex
            if ok:
                sub.Commit()
                o.status = u'SUCCESS'
            else:
                sub.RollBack()
                o.notes.append(u'Native circuit length NOT updated: ' + message)
        t.Commit()
    except Exception:
        if t.HasStarted() and not t.HasEnded():
            t.RollBack()
        raise
    for o in routed:
        try:
            o.length_after = o.circuit.Length
        except Exception:
            pass


# ============================================================================== report
LINE = u'=' * 50
DASH = u'-' * 50
LOG_PATH = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'EleTools', 'CircuitLength', 'last_run.txt')


def _esc(text):
    return (u'%s' % text).replace(u'&', u'&amp;').replace(u'<', u'&lt;').replace(u'>', u'&gt;')


def _m(feet):
    return u'%.2f m' % meters(feet)


class Log(object):
    """Everything shown in the output window also goes to %APPDATA%\\EleTools\\CircuitLength\\last_run.txt."""

    def __init__(self, output):
        self.output = output
        self.lines = []

    def block(self, text):
        self.lines.append(text)
        if self.output is not None:
            self.output.print_html(u'<pre>%s</pre>' % _esc(text))

    def html(self, html):
        if self.output is not None:
            self.output.print_html(html)

    def save(self):
        try:
            folder = os.path.dirname(LOG_PATH)
            if not os.path.isdir(folder):
                os.makedirs(folder)
            f = io.open(LOG_PATH, 'w', encoding='utf-8')
            try:
                f.write(u'%s\n' % time.strftime('%Y-%m-%d %H:%M:%S'))
                f.write(u'\n'.join(self.lines) + u'\n')
            finally:
                f.close()
        except Exception:
            pass


def circuit_name(circuit):
    number = getattr(circuit, 'CircuitNumber', u'') or u''
    load = getattr(circuit, 'LoadName', u'') or u''
    return (u'%s  (%s)' % (number, load)) if load else (number or u'Id %s' % _eid(circuit.Id))


def report(log, outcomes, info):
    log.block(u'%s\n3-PHASE CIRCUIT LENGTH UPDATE\n%s' % (LINE, LINE))
    log.block(u'\n'.join(info['sources']))
    for o in outcomes:
        lines = [u'Circuit: ' + circuit_name(o.circuit)]
        if o.room:
            lines.append(u'Panel room: ' + o.room)
        if o.status == u'FAILED':
            if o.panel:
                lines.append(u'Panel: ' + o.panel)
            lines += [u'Status: FAILED', u'Reason: ' + o.reason]
        else:
            s = o.sums
            lines += [u'Panel: ' + o.panel, u'Destination: ' + o.destination, u'']
            lines.append(u'Horizontal Tray : ' + _m(s.get('horizontal', 0.0)))
            lines.append(u'Vertical Riser  : ' + _m(s.get('vertical', 0.0)))
            if s.get('other', 0.0) > 1e-6:
                lines.append(u'Other Tray      : ' + _m(s.get('other', 0.0)))
            conn = s.get('connection', 0.0)
            lines.append(u'Connections     : %s%s' % (_m(conn), u'' if INCLUDE_CONNECTIONS else u'  (not in total)'))
            lines.append(DASH)
            lines.append(u'Total Route     : ' + _m(o.total))
            if o.length_before is not None:
                after = (u'  ->  %s' % _m(o.length_after)) if o.length_after is not None and o.status == u'SUCCESS' else u''
                lines.append(u'Revit Length    : %s%s' % (_m(o.length_before), after))
            lines.append(u'Path nodes      : %d (path length %s)' % (len(o.points), _m(path_length(o.points))))
            elements = set(st[2] for st in o.steps if st[2] is not None)
            links = sorted(set(k[0] for k in elements if k[0] != u'host'))
            if links:
                lines.append(u'Tray source     : linked model (%s)' % u', '.join(
                    info.get('link_names', {}).get(k, k) for k in links))
            lines.append(u'Tray elements   : %d' % len(elements))
            lines.append(u'Status          : ' + o.status)
        for n in o.notes:
            lines.append(u'Note: ' + n)
        lines.append(u'Element Id      : %s' % _eid(o.circuit.Id))
        log.block(u'\n'.join(lines))
        try:
            log.html(u'<div>%s</div>' % log.output.linkify(o.circuit.Id, u'Select circuit'))
        except Exception:
            pass
        log.block(DASH)
    count = defaultdict(int)
    for o in outcomes:
        count[o.status] += 1
    log.block(u'%s\n%s\n%s\n%s' % (LINE, info['summary'], u'   '.join(
        u'%s: %d' % (k, count[k]) for k in (u'SUCCESS', u'CALCULATED', u'FAILED') if count[k]), LINE))


def _sources_text(counts, skipped):
    lines = [u'Cable trays read from:']
    for s, trays, fittings in counts:
        lines.append(u'  %s   %d trays, %d fittings' % (s.name, trays, fittings))
    if skipped:
        lines.append(u'%d link(s) not loaded - not offered (load a link in Manage Links if its trays are needed).'
                     % len(skipped))
    return lines


# ============================================================================== run (used by the window)
def run(doc, circuits, how, sources, skipped, log):
    """Route + update. Returns the outcomes (also written to the log)."""
    t1 = time.time()
    graph, counts = build_graph(sources)
    if not graph.edges:
        log.block(u'The selected model(s) have no usable cable trays.')
        return []
    room_info, n_rooms = {}, 0
    if USE_ROOM_RISERS:
        rooms = load_rooms(doc)
        n_rooms = len(rooms)
        room_info = join_panel_rooms(graph, circuits, rooms)
    outcomes = [solve(graph, c) for c in sorted(circuits, key=lambda c: (c.PanelName or u'', c.CircuitNumber or u''))]
    for o in outcomes:
        panel = o.circuit.BaseEquipment
        if panel is None or not USE_ROOM_RISERS:
            continue
        ri = room_info.get(_eid(panel.Id))
        o.room = (u'%s - %d vertical tray piece(s) = %d riser column(s), %d join(s) to them' % ri) if ri else \
            u'panel is not inside a room of the loaded models (no room riser rule)'
    update_model(doc, outcomes)
    info = {
        'sources': _sources_text(counts, skipped),
        'link_names': dict((s.key, s.name) for s in sources),
        'summary': u'%d circuit(s) %s | tray network: %d segments, %d joints, %d gaps joined | %.1f s%s' % (
            len(circuits), how, len([e for e in graph.edges if not e.dead]), len(graph.nodes),
            graph.bridges, time.time() - t1, u'' if UPDATE_NATIVE_LENGTH else u' | REPORT ONLY') + (
            u' | %d rooms read' % n_rooms if USE_ROOM_RISERS else u''),
    }
    report(log, outcomes, info)
    return outcomes


# ============================================================================== the window
XAML = os.path.join(os.path.dirname(__file__), 'ui.xaml')

# window field -> (module setting, default)
NUMBERS = (('txtPlan', 'CONNECT_TOL_MM'), ('txtDrop', 'MAX_DROP_MM'), ('txtGap', 'BRIDGE_TOL_MM'),
           ('txtRiserGap', 'RISER_GAP_MM'), ('txtRoomReach', 'ROOM_RISER_REACH_MM'))
SWITCHES = (('chkRooms', 'USE_ROOM_RISERS'), ('chkPanelTop', 'PANEL_TOP_TRAYS'), ('chkConnections', 'INCLUDE_CONNECTIONS'),
            ('chkUpdate', 'UPDATE_NATIVE_LENGTH'))
FILTERS = ((u'All circuits', u''), (u'Failed only', u"Status = 'FAILED'"),
           (u'Success only', u"Status = 'SUCCESS'"), (u'Calculated only', u"Status = 'CALCULATED'"))


def _num(value):
    return u'%.2f' % meters(value) if value is not None else u''


class LengthWindow(forms.WPFWindow):
    def __init__(self, doc, uidoc):
        forms.WPFWindow.__init__(self, XAML)
        import clr
        clr.AddReference('System.Data')        # FIX: System.Data is not loaded by default in pyRevit's IronPython
        from System.Windows import Visibility
        from System.Windows.Controls import CheckBox
        from System.Data import DataTable
        self._Visibility, self._CheckBox = Visibility, CheckBox
        self.doc, self.uidoc = doc, uidoc
        self.cfg = script.get_config()
        self.outcomes = []

        # ---- panels (+ what is selected in Revit)
        selected_circuits, selected_panels = {}, set()
        for i in uidoc.Selection.GetElementIds():
            el = doc.GetElement(i)
            if isinstance(el, ElectricalSystem):
                selected_circuits[_eid(el.Id)] = el
            elif el is not None:
                if _is_panel(el):
                    selected_panels.add(_eid(el.Id))
                for c in _fed_by(el):
                    if not _is_panel(el):
                        selected_circuits[_eid(c.Id)] = c
        self.groups = []                      # [(label, [circuits], CheckBox)]
        sel = [c for c in selected_circuits.values() if is_three_phase(c)]
        if sel:
            self._add_group(u'Selected in Revit (%d three-phase circuits)' % len(sel), sel, True)
        by_panel = {}
        for c in DB.FilteredElementCollector(doc).OfClass(ElectricalSystem).ToElements():
            if not is_three_phase(c) or c.BaseEquipment is None:
                continue
            key = _eid(c.BaseEquipment.Id)
            if key not in by_panel:
                by_panel[key] = [_panel_label(c.BaseEquipment), []]
            by_panel[key][1].append(c)
        for key, (label, circuits) in sorted(by_panel.items(), key=lambda kv: kv[1][0]):
            self._add_group(u'%s   (%d)' % (label, len(circuits)), circuits, key in selected_panels)
        self.txtSearch.TextChanged += self._filter_panels
        self.btnAll.Click += lambda s, a: self._tick_panels(True)
        self.btnNone.Click += lambda s, a: self._tick_panels(False)
        self._count_panels()

        # ---- tray models (remembered)
        self.sources, self.skipped = tray_sources(doc)
        try:
            last = list(self.cfg.get_option('tray_models', []))
        except Exception:
            last = []
        self.model_boxes = []
        for s in self.sources:
            box = CheckBox()
            box.Content = s.label
            box.IsChecked = (s.title in last) if last else (len(self.sources) == 1)
            self.lstModels.Items.Add(box)
            self.model_boxes.append((box, s))
        if not self.sources:
            self.lblModels.Text = u'No cable trays in this model or its loaded links - load the MEP / containment link.'
        else:
            self.lblModels.Text = u'%d model(s) with cable trays.%s' % (len(self.sources), (
                u'  %d link(s) not loaded are not listed.' % len(self.skipped)) if self.skipped else u'')

        # ---- options (remembered)
        g = globals()
        for field, name in NUMBERS:
            getattr(self, field).Text = u'%g' % float(self._opt(name, g[name]))
        for field, name in SWITCHES:
            getattr(self, field).IsChecked = bool(self._opt(name, g[name]))

        # ---- results
        self.table = DataTable('results')
        for col in ('Panel', 'Circuit', 'Destination', 'Horizontal', 'Riser', 'Other', 'Drops', 'Total',
                    'Before', 'After', 'Status', 'Note', 'Id'):
            self.table.Columns.Add(col)
        self.gridResults.ItemsSource = self.table.DefaultView
        for label, _f in FILTERS:
            self.cmbFilter.Items.Add(label)
        self.cmbFilter.SelectedIndex = 0
        self.cmbFilter.SelectionChanged += self._filter_results
        self.gridResults.MouseDoubleClick += self._select_in_revit
        self.btnSelect.Click += self._select_in_revit
        self.btnRun.Click += self._run
        self.btnLog.Click += self._open_log
        self.btnClose.Click += lambda s, a: self.Close()
        self.lblStatus.Text = u'Tick the panels and the cable tray model, then Run.'

    # -- helpers
    def _opt(self, name, default):
        try:
            return self.cfg.get_option(name.lower(), default)
        except Exception:
            return default

    def _add_group(self, label, circuits, checked):
        box = self._CheckBox()
        box.Content = label
        box.IsChecked = checked
        box.Click += lambda s, a: self._count_panels()
        self.lstPanels.Items.Add(box)
        self.groups.append((label, circuits, box))

    def _filter_panels(self, sender, args):
        words = self.txtSearch.Text.upper().split()
        for label, _c, box in self.groups:
            show = all(w in label.upper() for w in words)
            box.Visibility = self._Visibility.Visible if show else self._Visibility.Collapsed

    def _tick_panels(self, value):
        for _l, _c, box in self.groups:
            if box.Visibility == self._Visibility.Visible:
                box.IsChecked = value
        self._count_panels()

    def _chosen_circuits(self):
        seen = {}
        for _l, circuits, box in self.groups:
            if box.IsChecked:
                for c in circuits:
                    seen[_eid(c.Id)] = c
        return list(seen.values())

    def _count_panels(self):
        ticked = len([1 for _l, _c, b in self.groups if b.IsChecked])
        self.lblPanels.Text = u'%d ticked, %d circuits' % (ticked, len(self._chosen_circuits()))

    def _filter_results(self, sender, args):
        self.table.DefaultView.RowFilter = FILTERS[max(0, self.cmbFilter.SelectedIndex)][1]

    def _read_options(self):
        g = globals()
        for field, name in NUMBERS:
            try:
                value = float(getattr(self, field).Text.replace(u',', u'.'))
                if value <= 0:
                    raise ValueError()
            except Exception:
                labels = {'CONNECT_TOL_MM': u'Panel / device to tray - plan', 'MAX_DROP_MM': u'Panel / device to tray - height',
                          'BRIDGE_TOL_MM': u'Join open tray ends', 'RISER_GAP_MM': u'Join riser cut at slab',
                          'ROOM_RISER_REACH_MM': u'Electrical room: tray to riser'}
                forms.alert(u'Enter a positive number of mm for "%s".' % labels.get(name, name), title='Circuit length')
                return False
            g[name] = value
            self.cfg.set_option(name.lower(), value)
        for field, name in SWITCHES:
            g[name] = bool(getattr(self, field).IsChecked)
            self.cfg.set_option(name.lower(), g[name])
        return True

    def _busy(self, text):
        self.lblStatus.Text = text
        try:
            from System.Windows.Threading import DispatcherPriority
            from System import Action
            self.Dispatcher.Invoke(Action(lambda: None), DispatcherPriority.Background)
        except Exception:
            pass

    # -- actions
    def _run(self, sender, args):
        circuits = self._chosen_circuits()
        sources = [s for box, s in self.model_boxes if box.IsChecked]
        if not circuits:
            forms.alert(u'Tick at least one panel.', title='Circuit length')
            return
        if not sources:
            forms.alert(u'Tick the model with the cable trays.', title='Circuit length')
            return
        if not self._read_options():
            return
        try:
            self.cfg.tray_models = [s.title for s in sources]
            script.save_config()
        except Exception:
            pass
        ticked = len([1 for _l, _c, b in self.groups if b.IsChecked])
        self._busy(u'Reading %d cable tray model(s) and routing %d circuit(s) ...' % (len(sources), len(circuits)))
        log = Log(None)
        try:
            self.outcomes = run(self.doc, circuits, u'from %d panel(s)' % ticked, sources, self.skipped, log)
        except Exception:
            import traceback
            log.block(u'ERROR:\n' + traceback.format_exc())
            self.outcomes = []
            forms.alert(u'Error - see the log.', title='Circuit length')
        finally:
            log.save()
        self._show(self.outcomes)

    def _show(self, outcomes):
        self.table.Rows.Clear()
        count = defaultdict(int)
        for o in outcomes:
            count[o.status] += 1
            s = o.sums or {}
            note = o.reason if o.status == u'FAILED' else u''
            if o.notes:
                note = (note + u' ' if note else u'') + u' '.join(o.notes)
            row = self.table.NewRow()
            row['Panel'] = o.panel
            row['Circuit'] = getattr(o.circuit, 'CircuitNumber', u'') or u''
            row['Destination'] = o.destination or (getattr(o.circuit, 'LoadName', u'') or u'')
            ok = o.status != u'FAILED'
            row['Horizontal'] = _num(s.get('horizontal', 0.0)) if ok else u''
            row['Riser'] = _num(s.get('vertical', 0.0)) if ok else u''
            row['Other'] = _num(s.get('other', 0.0)) if ok and s.get('other', 0.0) > 1e-6 else u''
            row['Drops'] = _num(s.get('connection', 0.0)) if ok else u''
            row['Total'] = _num(o.total) if ok else u''
            row['Before'] = _num(o.length_before)
            row['After'] = _num(o.length_after) if o.status == u'SUCCESS' else u''
            row['Status'] = o.status
            row['Note'] = note
            row['Id'] = u'%s' % _eid(o.circuit.Id)
            self.table.Rows.Add(row)
        self.lblSummary.Text = u'   '.join(u'%s: %d' % (k, count[k]) for k in (u'SUCCESS', u'CALCULATED', u'FAILED') if count[k])
        self.lblStatus.Text = u'Done - %d circuit(s). Log: %s' % (len(outcomes), LOG_PATH)

    def _select_in_revit(self, sender, args):
        ids = List[DB.ElementId]()
        for item in self.gridResults.SelectedItems:
            try:
                from System import Int64
                ids.Add(DB.ElementId(Int64(int(item['Id']))))
            except Exception:
                pass
        if ids.Count:
            self.uidoc.Selection.SetElementIds(ids)
            try:
                self.uidoc.ShowElements(ids)
            except Exception:
                pass
            self.lblStatus.Text = u'%d circuit(s) selected in Revit.' % ids.Count

    def _open_log(self, sender, args):
        if os.path.exists(LOG_PATH):
            from System.Diagnostics import Process
            Process.Start(LOG_PATH)
        else:
            forms.alert(u'No log yet - Run first.', title='Circuit length')


def main():
    try:
        window = LengthWindow(revit.doc, revit.uidoc)
    except Exception:
        # an error while opening the window goes to the log too (it came before any log was written)
        import traceback
        log = Log(None)
        log.block(u'ERROR while opening the window:\n' + traceback.format_exc())
        log.save()
        forms.alert(u'The window could not open - details in the log:\n%s' % LOG_PATH, title='Circuit length')
        return
    window.ShowDialog()


if __name__ == '__main__':
    main()
