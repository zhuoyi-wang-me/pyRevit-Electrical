# -*- coding: utf-8 -*-
"""
Panel selection.

For each planned circuit choose a panel that
    1. has the panel type mapped from the load classification,
    2. belongs to the circuit's region (unless cross-region is enabled and no
       in-region panel can take it),
    3. has enough spare poles and stays under the max-utilisation cap,
    4. has the lowest utilisation (ties: same level if preferred, then distance).

Large / dedicated circuits are placed first (longest-processing-time heuristic)
so they are not starved of poles by many small circuits.
"""
from __future__ import division

from autocircuit.geometry import dist2d
from autocircuit.models import STATUS_NO_PANEL, STATUS_NO_CAPACITY


def apply_region_overrides(panels, overrides, logger=None):
    """Explicit region->panel mapping replaces the location-derived region of a panel."""
    by_name = dict((p.name.strip().upper(), p) for p in panels)
    claimed = {}
    for region, names in (overrides or {}).items():
        for name in names or []:
            panel = by_name.get((name or '').strip().upper())
            if panel is None:
                if logger:
                    logger.warning('Region override "%s": panel "%s" not found', region, name)
                continue
            claimed.setdefault(panel.element_id, set()).add(region)
    for p in panels:
        if p.element_id in claimed:
            p.regions = claimed[p.element_id]
    return panels


def filter_panels_to_scope(panels, scope_names):
    """Keep only panels that serve at least one in-scope region (by location or override)."""
    scope = set(scope_names or [])
    return [p for p in panels if p.regions & scope]


class PanelSelector(object):

    def __init__(self, panels, settings, logger=None):
        g = settings['general']
        self.panels = [p for p in panels if not p.locked]
        self.allow_cross = bool(g.get('allow_cross_region', False))
        self.prefer_level = bool(g.get('prefer_same_level', False))
        self.max_util = float(g.get('max_panel_utilization_pct', 100) or 100) / 100.0
        self.log = logger

    def assign(self, circuits, plan):
        by_type = {}
        for p in self.panels:
            if p.panel_type:
                by_type.setdefault(p.panel_type, []).append(p)

        order = sorted(circuits, key=lambda c: (0 if c.dedicated else 1, -c.poles, -c.load_va, c.uid))
        for c in order:
            candidates = by_type.get((c.panel_type or '').upper(), [])
            if not candidates:
                c.fail(STATUS_NO_PANEL, 'No {0} panel exists in the model'.format(c.panel_type))
                plan.warn('Circuit #{0} ({1}): no panel of type {2} in the model.'.format(
                    c.uid, ', '.join(c.room_names), c.panel_type))
                continue

            local = [p for p in candidates if c.region in p.regions]
            pool = self._available(local, c)
            cross = False
            if not pool and self.allow_cross:
                pool = self._available([p for p in candidates if c.region not in p.regions], c)
                cross = bool(pool)

            if not pool:
                if not local and not self.allow_cross:
                    c.fail(STATUS_NO_PANEL, 'No {0} panel assigned to region "{1}"'.format(c.panel_type, c.region))
                else:
                    c.fail(STATUS_NO_CAPACITY, 'No {0} panel with {1} spare pole(s) under {2:.0f}% utilisation'.format(
                        c.panel_type, c.poles, self.max_util * 100))
                plan.warn('Circuit #{0} ({1}, region {2}): {3} - skipped.'.format(
                    c.uid, ', '.join(c.room_names), c.region, c.message))
                continue

            chosen = pool[0]
            chosen.reserve(c.poles, c.load_va)
            c.panel = chosen
            c.alternates = pool[1:]
            if cross:
                c.message = 'Cross-region: region "{0}" served by {1}'.format(c.region, chosen.name)
                plan.warn('Circuit #{0}: {1}'.format(c.uid, c.message))

    def _available(self, pool, c):
        ok = [p for p in pool
              if p.max_poles > 0 and p.free_poles >= c.poles
              and p.utilization_after(c.poles) <= self.max_util + 1e-9]

        def rank(p):
            level_penalty = 0 if (not self.prefer_level or p.level_id == c.level_id) else 1
            return (level_penalty, round(p.utilization, 6), dist2d(p.point, c.centroid), p.name)
        return sorted(ok, key=rank)
