# -*- coding: utf-8 -*-
"""
Orchestration: collect -> plan -> (optionally) build.

`analyse()` is strictly read-only (Preview Mode). `execute()` re-analyses the
current model state (so a stale preview can never be committed) and then builds.
"""
import datetime
import time

from autocircuit.circuit_builder import CircuitBuilder
from autocircuit.collectors import (RoomResolver, FamilyConnectorCache, ProjectClassificationResolver,
                                    collect_fixtures, collect_panels, collect_regions)
from autocircuit.mapping import sync_mapping
from autocircuit.geometry import RegionIndex
from autocircuit.planner import CircuitPlanner


class ScopeError(Exception):
    """User-facing problem with the selected-region scope (shown without a stack trace)."""


class AutoCircuitEngine(object):

    def __init__(self, doc, settings, logger):
        self.doc = doc
        self.settings = settings
        self.log = logger

    # ---------------------------------------------------------------- scope
    def scope(self):
        """(selected_only, unique_ids, by_room)"""
        sc = self.settings.get('scope', {})
        return (sc.get('mode') == 'selected_regions', list(sc.get('selected_region_ids') or []),
                sc.get('inclusion', 'fixture') == 'room')

    def _region_index(self):
        rs = self.settings.get('regions', {})
        selected, ids, _ = self.scope()
        if selected:
            if not ids:
                raise ScopeError('No filled region is selected. Use "Pick Filled Region(s)" or choose "Whole model".')
            regions = collect_regions(self.doc, rs, self.log, only_unique_ids=ids)
            if not regions:
                raise ScopeError('The selected filled region(s) no longer exist or are not in plan views. '
                                 'Pick the region(s) again.')
            if len(regions) < len(ids):
                self.log.warning('%d selected filled region(s) could not be used', len(ids) - len(regions))
        else:
            regions = collect_regions(self.doc, rs, self.log)
        return RegionIndex(regions, bool(rs.get('level_filter', True))), regions

    def analyse(self):
        t0 = time.time()
        now = datetime.datetime.now()
        idx, regions = self._region_index()
        t_reg = time.time()

        g = self.settings['general']
        selected, _, by_room = self.scope()
        stats = {}
        resolver = RoomResolver(self.doc, self.settings, self.log)
        classes = ProjectClassificationResolver(self.doc, self.log)
        fam_cache = FamilyConnectorCache(self.doc, self.log) \
            if g.get('read_classification_from_family', False) else None
        fixtures = collect_fixtures(self.doc, self.settings, resolver, idx, fam_cache, self.log,
                                    classes if g.get('probe_classification_from_project', True) else None,
                                    scope_only=selected, region_by_room=by_room, stats=stats)
        resolver.finalize()
        t_fix = time.time()

        # the mapping always reflects the project's load classification list
        self.settings['classification_map'], added, _ = sync_mapping(
            self.settings.get('classification_map', []), classes.names,
            self.settings.get('classification_suggestions', []))

        # names that do not exist as a project Load Classification are flagged
        unknown = {}
        for f in fixtures:
            if not f.classification:
                continue
            canon = classes.canonical(f.classification)
            if canon:
                f.classification = canon
            else:
                unknown[f.classification] = unknown.get(f.classification, 0) + 1

        panels = collect_panels(self.doc, self.settings, idx, self.log,
                                scope_names=idx.names() if selected else None)
        plan = CircuitPlanner(self.settings, self.log).plan(fixtures, panels, resolver.nodes)
        t_plan = time.time()

        for name in sorted(unknown):
            plan.warn('Load classification "{0}" ({1} device(s)) is not defined in the project '
                      '(Electrical Settings > Load Classifications).'.format(name, unknown[name]))
        if added:
            plan.warn('Project classification(s) not yet configured, suggested mapping used: ' + ', '.join(added))
        if not classes.names:
            plan.warn('The project has no Electrical Load Classifications defined.')

        plan.run_id = now.strftime('%Y%m%d-%H%M%S')
        plan.started = now.strftime('%Y-%m-%d %H:%M:%S')
        scope_text = ('Selected filled region(s) only: ' + ', '.join(
            '{0} ({1})'.format(r.name, r.level_name) for r in regions)) if selected else 'Whole model'
        plan.info = {
            'Scope': scope_text,
            'Devices outside scope (ignored)': stats.get('outside_scope', 0) if selected else 'n/a',
            'Model': self.doc.Title,
            'Regions found': len(regions),
            'Rooms/spaces referenced': len(resolver.nodes),
            'Panels found': len(panels),
            'Load classifications in project': len(classes.names),
            'Family types classified from project': classes.probed,
            'Family types that could not be probed': classes.failed,
            'Families opened for classification': fam_cache.opened if fam_cache else 0,
        }
        if selected and not panels:
            plan.warn('No panel is inside the selected region(s). Place the panels inside the region, or list '
                      'them for the region under Region Settings > OverridePanels.')
        if selected and not fixtures:
            plan.warn('No circuitable device was found inside the selected region(s) on their level(s). '
                      'If the region should apply to all levels, untick "Region applies only to its view\'s level".')
        if not regions:
            plan.warn('No distribution-zone filled regions found (type prefix "{0}") - all elements '
                      'are treated as one region.'.format(
                          self.settings.get('regions', {}).get('filled_region_type_prefix', '')))
        plan.duration_s = time.time() - t0
        self.log.info('Analyse: regions %.1fs, fixtures %.1fs, plan %.1fs, %d fixtures, %d circuits',
                      t_reg - t0, t_fix - t_reg, t_plan - t_fix, len(fixtures), len(plan.circuits))
        return plan

    def execute(self):
        t0 = time.time()
        plan = self.analyse()
        plan.mode = 'execute'
        CircuitBuilder(self.doc, self.settings, self.log).execute(plan)
        plan.duration_s = time.time() - t0
        return plan

    # ---------------------------------------------------------- UI helpers
    def scan_regions(self):
        """Rows for the Region Settings grid: region, levels, panels located inside."""
        idx, regions = self._region_index()
        panels = collect_panels(self.doc, self.settings, idx, self.log, apply_overrides=False)
        levels = {}
        for r in regions:
            levels.setdefault(r.name, set()).add(r.level_name)
        unassigned = self.settings['general'].get('unassigned_region_name', '<No Region>')
        names = sorted(levels) + ([] if self.scope()[0] else [unassigned])
        rows = []
        for n in names:
            located = sorted(p.name for p in panels if n in p.regions)
            rows.append({'region': n, 'levels': ', '.join(sorted(levels.get(n, []))),
                         'panels': ', '.join(located)})
        return rows

    def model_classifications(self):
        """Names of the project's Electrical Load Classifications."""
        return ProjectClassificationResolver(self.doc, self.log).names
