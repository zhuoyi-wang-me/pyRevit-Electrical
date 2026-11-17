# -*- coding: utf-8 -*-
"""
Read-only model extraction: fixtures, rooms/spaces (host + linked), distribution
regions (filled regions) and panels -> plain models.

Nothing here opens a transaction. The only heavy call is Document.EditFamily(),
used optionally (cached once per family) to read a connector's Load
Classification when it isn't exposed as an instance/type parameter.
"""
from __future__ import division

import re

import clr
clr.AddReference('RevitAPI')
import Autodesk.Revit.DB as DB
from Autodesk.Revit.DB import (BuiltInCategory, BuiltInParameter, FilteredElementCollector,
                               ElementMulticategoryFilter, FamilyInstance, FilledRegion,
                               RevitLinkInstance, XYZ, StorageType, ConnectorElement,
                               SpatialElementBoundaryOptions, Domain, Transaction, TransactionStatus,
                               BoundingBoxIntersectsFilter, Outline)
from Autodesk.Revit.DB.Electrical import ElectricalLoadClassification
from System.Collections.Generic import List

from autocircuit import revit_compat as rc
from autocircuit.circuit_builder import FailureCollector
from autocircuit.geometry import Region
from autocircuit.models import FixtureInfo, PanelInfo, RoomNode, link_adjacency
from autocircuit.panel_selector import apply_region_overrides, filter_panels_to_scope

MM_TO_FT = 1.0 / 304.8
_PANEL_PART_TYPES = ('PanelBoard', 'Switchboard', 'OtherPanel')
_DISTRIBUTION_PART_TYPES = _PANEL_PART_TYPES + ('Transformer',)


# ------------------------------------------------------------------- helpers
def _bic(name):
    return getattr(BuiltInCategory, name, None)


def category_is(el, bic_name):
    bic = _bic(bic_name)
    if bic is None or el.Category is None:
        return False
    return rc.eid_int(el.Category.Id) == int(bic)


def _bip(name):
    return getattr(BuiltInParameter, name, None)


def get_type(doc, el):
    try:
        return doc.GetElement(el.GetTypeId())
    except Exception:
        return None


def find_param(doc, el, names=(), bips=()):
    """First populated parameter: built-ins, then named instance, then named type params."""
    for name in bips:
        bip = _bip(name)
        p = el.get_Parameter(bip) if bip is not None else None
        if p is not None and p.HasValue:
            return p
    owners = [el]
    etype = get_type(doc, el)
    if etype is not None:
        owners.append(etype)
    for owner in owners:
        for n in names:
            p = owner.LookupParameter(n)
            if p is not None and p.HasValue:
                return p
    return None


def param_text(doc, p):
    if p is None or not p.HasValue:
        return ''
    if p.StorageType == StorageType.String:
        return (p.AsString() or '').strip()
    if p.StorageType == StorageType.ElementId:
        target = doc.GetElement(p.AsElementId())
        return rc.element_name(target).strip() if target is not None else ''
    return (p.AsValueString() or '').strip()


def _part_type_name(el):
    try:
        p = el.Symbol.Family.get_Parameter(BuiltInParameter.FAMILY_CONTENT_PART_TYPE)
        if p is not None and p.HasValue:
            val = p.AsInteger()
            for n in _DISTRIBUTION_PART_TYPES:
                pt = getattr(DB.PartType, n, None)
                if pt is not None and int(pt) == val:
                    return n
    except Exception:
        pass
    return None


def is_panel(el):
    if not isinstance(el, FamilyInstance) or not category_is(el, 'OST_ElectricalEquipment'):
        return False
    if _part_type_name(el) in _PANEL_PART_TYPES:
        return True
    p = el.get_Parameter(BuiltInParameter.RBS_ELEC_MAX_POLE_BREAKERS)
    return p is not None and p.HasValue and p.AsInteger() > 0


def is_distribution_equipment(el):
    return is_panel(el) or (category_is(el, 'OST_ElectricalEquipment')
                            and _part_type_name(el) == 'Transformer')


def _in_secondary_option(el):
    try:
        opt = el.DesignOption
        return opt is not None and not opt.IsPrimary
    except Exception:
        return False


def _family_type_label(el):
    try:
        return u'{0} : {1}'.format(el.Symbol.Family.Name, rc.element_name(el.Symbol))
    except Exception:
        return rc.element_name(el)


# ------------------------------------------------------ family connector cache
class FamilyConnectorCache(object):
    """Reads Load Classification from the family's electrical connector (once per family)."""

    def __init__(self, doc, logger):
        self.doc = doc
        self.log = logger
        self._cache = {}
        self.opened = 0

    def classification(self, family):
        fid = rc.eid_int(family.Id)
        if fid in self._cache:
            return self._cache[fid]
        result, fdoc = '', None
        try:
            if family.IsInPlace or not family.IsEditable:
                self._cache[fid] = ''
                return ''
            fdoc = self.doc.EditFamily(family)
            self.opened += 1
            bip = _bip('RBS_ELEC_LOAD_CLASSIFICATION')
            for ce in FilteredElementCollector(fdoc).OfClass(ConnectorElement):
                if ce.Domain != Domain.DomainElectrical:
                    continue
                p = ce.get_Parameter(bip) if bip is not None else None
                if p is None:
                    p = ce.LookupParameter('Load Classification')
                result = param_text(fdoc, p)
                if result:
                    break
        except Exception as ex:
            self.log.warning('Could not read connectors of family "%s": %s', family.Name, ex)
        finally:
            if fdoc is not None:
                try:
                    fdoc.Close(False)
                except Exception:
                    pass
        self._cache[fid] = result
        return result



# ------------------------------------------- project load classifications
class ProjectClassificationResolver(object):
    """
    Load classifications taken from the PROJECT.

    * names(): the project's ElectricalLoadClassification elements (the list shown
      in Revit > Electrical Settings > Load Classifications).
    * resolve(): for every family TYPE among the devices, one instance is put on a
      temporary circuit inside a transaction that is always ROLLED BACK; the
      circuit's LoadClassifications property is exactly what Revit assigns when
      the device is circuited in this project. Cached per type, one transaction
      per run, the model is never changed.
    """

    def __init__(self, doc, logger):
        self.doc = doc
        self.log = logger
        self.names = sorted(set(rc.element_name(e) for e in
                                FilteredElementCollector(doc).OfClass(ElectricalLoadClassification)
                                if rc.element_name(e)))
        self._by_lower = dict((n.strip().lower(), n) for n in self.names)
        self._cache = {}
        self.probed = 0
        self.failed = 0

    def canonical(self, text):
        """Project spelling of a classification name, or None if not in the project."""
        return self._by_lower.get((text or '').strip().lower())

    def _from_text(self, text):
        text = (text or '').strip()
        if not text:
            return ''
        exact = self.canonical(text)
        if exact:
            return exact
        for part in re.split(r'[;,]', text):          # multi-connector devices
            found = self.canonical(part)
            if found:
                return found
        return text

    def _read(self, system):
        try:
            return self._from_text(system.LoadClassifications)
        except AttributeError:
            return self._from_text(param_text(self.doc, system.LookupParameter('Load Classification')))

    def resolve(self, pairs):
        """pairs: [(element, FixtureInfo)] - sets fixture.classification from the project."""
        todo = {}
        for el, _ in pairs:
            tid = rc.eid_int(el.GetTypeId())
            if tid not in self._cache and tid not in todo:
                todo[tid] = el
        if todo:
            self._probe(todo)
        for el, fi in pairs:
            name = self._cache.get(rc.eid_int(el.GetTypeId()))
            if name:
                fi.classification = name
                fi.classification_source = 'project'

    def _probe(self, todo):
        if self.doc.IsModifiable:
            self.log.warning('Classification probe skipped: a transaction is already open')
            return
        t = Transaction(self.doc, 'AutoCircuit: read load classifications (rolled back)')
        opts = t.GetFailureHandlingOptions()
        opts.SetFailuresPreprocessor(FailureCollector())
        opts.SetClearAfterRollback(True)
        t.SetFailureHandlingOptions(opts)
        t.Start()
        try:
            for tid, el in todo.items():
                name = ''
                try:
                    system = rc.create_power_system(self.doc, [el.Id])
                    if system is not None:
                        name = self._read(system)
                    self.probed += 1
                except Exception as ex:
                    self.failed += 1
                    self.log.debug('Classification probe failed for type %s: %s', tid, ex)
                self._cache[tid] = name
        finally:
            if t.GetStatus() == TransactionStatus.Started:
                t.RollBack()
        self.log.info('Classification probe: %d type(s) read, %d failed (rolled back)', self.probed, self.failed)


# ---------------------------------------------------------------- rooms/spaces
class RoomResolver(object):
    """
    Resolves the room of an element, in order:
      1. FamilyInstance.Room[phase]           (host rooms)
      2. Document.GetRoomAtPoint(probe)       (probe = level elevation + offset,
                                               catches ceiling lights above the
                                               room's upper limit)
      3. rooms of loaded Revit links          (point transformed into the link)
      4. MEP Spaces                           (host)
    """

    def __init__(self, doc, settings, logger):
        g = settings['general']
        self.doc = doc
        self.log = logger
        self.use_spaces = bool(g.get('use_spaces', True))
        self.probe = float(g.get('room_probe_height_mm', 300)) * MM_TO_FT
        self.phase = None
        for ph in doc.Phases:          # last phase = "New Construction" in most templates
            self.phase = ph
        self.nodes = {}
        self._level_z = {}
        self._opts = SpatialElementBoundaryOptions()
        self.has_local_rooms = FilteredElementCollector(doc).OfCategory(
            BuiltInCategory.OST_Rooms).WhereElementIsNotElementType().GetElementCount() > 0
        self.links = []
        if g.get('use_linked_rooms', True):
            for li in FilteredElementCollector(doc).OfClass(RevitLinkInstance):
                ldoc = li.GetLinkDocument()
                if ldoc is None:
                    continue
                if FilteredElementCollector(ldoc).OfCategory(
                        BuiltInCategory.OST_Rooms).WhereElementIsNotElementType().GetElementCount() == 0:
                    continue
                tr = li.GetTotalTransform()
                self.links.append((rc.eid_int(li.Id), ldoc, tr, tr.Inverse))
        self.log.info('RoomResolver: host rooms=%s, links with rooms=%d', self.has_local_rooms, len(self.links))

    def _z(self, level_id, fallback):
        if level_id is None:
            return fallback
        if level_id not in self._level_z:
            lvl = self.doc.GetElement(rc.to_eid(level_id))
            self._level_z[level_id] = lvl.ProjectElevation if lvl is not None else fallback
        return self._level_z[level_id]

    def resolve(self, fi, pt, level_id):
        probe = XYZ(pt.X, pt.Y, self._z(level_id, pt.Z) + self.probe)
        room = None
        try:
            room = fi.get_Room(self.phase) if self.phase is not None else fi.Room
        except Exception:
            room = None
        if room is None and self.has_local_rooms:
            try:
                room = self.doc.GetRoomAtPoint(probe, self.phase) if self.phase else self.doc.GetRoomAtPoint(probe)
            except Exception:
                room = None
        if room is not None:
            return self._node(room, 0, None, 'R')

        for link_id, ldoc, tr, inv in self.links:
            try:
                lroom = ldoc.GetRoomAtPoint(inv.OfPoint(probe))
            except Exception:
                lroom = None
            if lroom is not None:
                return self._node(lroom, link_id, tr, 'R')

        if self.use_spaces:
            space = None
            try:
                space = fi.get_Space(self.phase) if self.phase is not None else fi.Space
            except Exception:
                space = None
            if space is None:
                try:
                    space = self.doc.GetSpaceAtPoint(probe, self.phase)
                except Exception:
                    space = None
            if space is not None:
                return self._node(space, 0, None, 'S')
        return None

    def _node(self, sp, link_id, transform, prefix):
        key = '{0}{1}:{2}'.format(prefix, link_id, rc.eid_int(sp.Id))
        node = self.nodes.get(key)
        if node is not None:
            return node
        number = param_text(sp.Document, sp.get_Parameter(BuiltInParameter.ROOM_NUMBER))
        name = param_text(sp.Document, sp.get_Parameter(BuiltInParameter.ROOM_NAME))
        label = (u'{0} {1}'.format(number, name)).strip() or rc.element_name(sp)
        if link_id:
            label += u' [link]'
        centroid = None
        if isinstance(sp.Location, DB.LocationPoint):
            p = sp.Location.Point
            if transform is not None:
                p = transform.OfPoint(p)
            centroid = (p.X, p.Y, p.Z)
        bkeys = set()
        try:
            for loop in sp.GetBoundarySegments(self._opts) or []:
                for seg in loop:
                    eid = seg.ElementId
                    if eid is not None and rc.eid_int(eid) > 0:
                        bkeys.add('{0}:{1}'.format(link_id, rc.eid_int(eid)))
        except Exception as ex:
            self.log.debug('Boundary read failed for %s: %s', label, ex)
        node = RoomNode(key, label, None, centroid, bkeys)
        self.nodes[key] = node
        return node

    def finalize(self):
        link_adjacency(self.nodes)


# -------------------------------------------------------------------- regions
def _region_name(doc, fr, by_comments):
    ftype = doc.GetElement(fr.GetTypeId())
    tname = param_text(doc, ftype.get_Parameter(BuiltInParameter.ALL_MODEL_TYPE_NAME)) or rc.element_name(ftype)
    comments = param_text(doc, fr.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS))
    return (comments if (by_comments and comments) else tname), tname


def filled_region_label(doc, unique_id, by_comments=True):
    """'Zone A (Level 01)' for display; None if the element no longer exists."""
    fr = doc.GetElement(unique_id)
    if not isinstance(fr, FilledRegion):
        return None
    name, _ = _region_name(doc, fr, by_comments)
    level = getattr(doc.GetElement(fr.OwnerViewId), 'GenLevel', None)
    return u'{0} ({1})'.format(name, rc.element_name(level) if level is not None else 'no level')


def _region_from(doc, fr, name, logger):
    level = getattr(doc.GetElement(fr.OwnerViewId), 'GenLevel', None)
    if level is None:
        logger.warning('Region "%s" (id %s) is not in a plan view - ignored', name, rc.eid_int(fr.Id))
        return None
    loops = []
    for loop in fr.GetBoundaries():
        pts = []
        for curve in loop:
            pts.extend((p.X, p.Y) for p in curve.Tessellate())
        loops.append(pts)
    return Region(name, loops, rc.eid_int(level.Id), rc.element_name(level), rc.eid_int(fr.Id))


def collect_regions(doc, region_settings, logger, only_unique_ids=None):
    """
    All distribution-zone filled regions (type-name prefix), or - when
    only_unique_ids is given - exactly the filled regions the user selected
    (prefix ignored; each selected region becomes a zone).
    """
    prefix = (region_settings.get('filled_region_type_prefix') or '').strip().upper()
    by_comments = region_settings.get('name_source', 'comments') == 'comments'
    if only_unique_ids is not None:
        elements = [doc.GetElement(u) for u in only_unique_ids]
        elements = [e for e in elements if isinstance(e, FilledRegion)]
    else:
        elements = FilteredElementCollector(doc).OfClass(FilledRegion)
    regions = []
    for fr in elements:
        try:
            name, tname = _region_name(doc, fr, by_comments)
            if only_unique_ids is None and prefix and not tname.upper().startswith(prefix):
                continue
            region = _region_from(doc, fr, name, logger)
            if region is not None:
                regions.append(region)
        except Exception as ex:
            logger.warning('Filled region %s skipped: %s', rc.eid_int(fr.Id), ex)
    logger.info('Collected %d region(s)%s', len(regions), ' (selected scope)' if only_unique_ids is not None else '')
    return regions


# ------------------------------------------------------------------- fixtures
def _classification(doc, el, g, family_cache, fallback):
    """Fallbacks used only when the project probe could not classify the element."""
    txt = param_text(doc, find_param(doc, el, g.get('classification_parameter_names', [])))
    if txt:
        return txt, 'parameter'
    if family_cache is not None:
        try:
            txt = family_cache.classification(el.Symbol.Family)
        except Exception:
            txt = ''
        if txt:
            return txt, 'family connector'
    if not g.get('use_category_fallback', False):
        return '', ''
    for cat_name, cls in (fallback or {}).items():
        if cls and category_is(el, cat_name):
            return cls, 'category fallback'
    return '', ''


def collect_fixtures(doc, settings, resolver, region_index, family_cache, logger, class_resolver=None,
                     scope_only=False, region_by_room=False, stats=None):
    """
    scope_only     : skip every element that is not inside a region of region_index
                     (used for "selected filled regions only"); skipped elements are
                     not read further and do not appear in the report.
    region_by_room : decide the region from the room's location point instead of the
                     device's own location (keeps rooms whole at region boundaries).
    """
    g = settings['general']
    cats = List[BuiltInCategory]()
    for name in g.get('fixture_categories', []):
        bic = _bic(name)
        if bic is None:
            logger.info('Category %s not available in this Revit version - ignored', name)
            continue
        cats.Add(bic)
    if cats.Count == 0:
        return []

    fallback = settings.get('category_fallback_classification', {})
    collector = FilteredElementCollector(doc).WherePasses(ElementMulticategoryFilter(cats)) \
        .WhereElementIsNotElementType().OfClass(FamilyInstance)
    bb = region_index.outline()
    if scope_only and not region_by_room and bb is not None:
        # quick spatial pre-filter: only elements whose bounding box touches the selected regions
        collector = collector.WherePasses(BoundingBoxIntersectsFilter(
            Outline(XYZ(bb[0], bb[1], -1.0e5), XYZ(bb[2], bb[3], 1.0e5))))
    outside = 0

    out, raw, level_names = [], [], {}
    for el in collector:
        try:
            if _in_secondary_option(el) or is_distribution_equipment(el):
                continue
            if not rc.power_connectors(el):
                continue
            pt = rc.element_point(el)
            if pt is None:
                continue
            lid = rc.element_level_id(el)
            lvl = rc.eid_int(lid) if lid is not None else None

            # ---- region / scope test first, so out-of-scope elements cost almost nothing
            node = None
            if region_by_room:
                node = resolver.resolve(el, pt, lvl)
                ref = node.centroid if (node is not None and node.centroid) else (pt.X, pt.Y, pt.Z)
                region = region_index.locate(ref[0], ref[1], lvl)
            else:
                region = region_index.locate(pt.X, pt.Y, lvl)
            if scope_only and region is None:
                outside += 1
                continue
            if node is None:
                node = resolver.resolve(el, pt, lvl)

            if lvl is not None and lvl not in level_names:
                level_names[lvl] = rc.element_name(doc.GetElement(lid))
            # parameter first, then the value on the family's power connector
            load = rc.param_number(find_param(doc, el, g.get('load_parameter_names', []),
                                              ('RBS_ELEC_APPARENT_LOAD',)), 'power') \
                or rc.connector_number(el, 'RBS_ELEC_APPARENT_LOAD', 'power') or 0.0
            volt = rc.param_number(find_param(doc, el, g.get('voltage_parameter_names', []),
                                              ('RBS_ELEC_VOLTAGE',)), 'voltage') \
                or rc.connector_number(el, 'RBS_ELEC_VOLTAGE', 'voltage') or 0.0
            poles = rc.param_number(find_param(doc, el, g.get('poles_parameter_names', []),
                                               ('RBS_ELEC_NUMBER_OF_POLES',))) \
                or rc.connector_number(el, 'RBS_ELEC_NUMBER_OF_POLES') or 1
            fi = FixtureInfo(
                element_id=rc.eid_int(el.Id), name=_family_type_label(el),
                category=el.Category.Name if el.Category else '', load_va=load, poles=int(poles), voltage=volt,
                point=(pt.X, pt.Y, pt.Z), level_id=lvl, level_name=level_names.get(lvl, ''),
                room_key=node.key if node else None, room_name=node.name if node else None,
                region=region,
                already_circuited=bool(rc.element_power_systems(el)),
                locked=rc.is_owned_by_other(doc, el.Id))
            raw.append((el, fi))
        except Exception as ex:
            logger.warning('Element %s skipped during collection: %s', rc.eid_int(el.Id), ex)

    # 1) load classification from the PROJECT (temporary circuit, rolled back)
    if class_resolver is not None:
        class_resolver.resolve([(el, fi) for el, fi in raw if not fi.already_circuited and not fi.locked])
    # 2) optional fallbacks for anything the project probe could not classify
    for el, fi in raw:
        if not fi.classification:
            try:
                fi.classification, fi.classification_source = _classification(doc, el, g, family_cache, fallback)
            except Exception as ex:
                logger.debug('Fallback classification failed for %s: %s', fi.element_id, ex)
        out.append(fi)
    if stats is not None:
        stats['outside_scope'] = outside
    logger.info('Collected %d circuitable element(s)%s', len(out),
                ', {0} outside scope ignored'.format(outside) if scope_only else '')
    return out


# --------------------------------------------------------------------- panels
def _panel_type(doc, el, name, param_name, prefix_map):
    if param_name:
        txt = param_text(doc, find_param(doc, el, [param_name]))
        if txt:
            return txt.upper()
    upper = (name or '').upper()
    for prefix in sorted(prefix_map or {}, key=len, reverse=True):
        if upper.startswith(prefix.upper()):
            return (prefix_map[prefix] or '').upper()
    return None


def collect_panels(doc, settings, region_index, logger, apply_overrides=True, scope_names=None):
    """scope_names: when set, only panels inside those regions (or listed for them
    under region_panel_overrides) are returned - all other panels are ignored."""
    g = settings['general']
    unassigned = g.get('unassigned_region_name', '<No Region>')
    panels = []
    for el in FilteredElementCollector(doc).OfCategory(BuiltInCategory.OST_ElectricalEquipment) \
            .WhereElementIsNotElementType().OfClass(FamilyInstance):
        try:
            if _in_secondary_option(el) or not is_panel(el):
                continue
            name = param_text(doc, el.get_Parameter(BuiltInParameter.RBS_ELEC_PANEL_NAME)) or rc.element_name(el)
            ptype = _panel_type(doc, el, name, g.get('panel_type_parameter'), g.get('panel_name_prefix_map'))
            mp = find_param(doc, el, bips=('RBS_ELEC_MAX_POLE_BREAKERS',))
            max_poles = mp.AsInteger() if mp is not None and mp.StorageType == StorageType.Integer else 0
            used = 0
            for s in rc.panel_assigned_systems(el):
                try:
                    used += int(s.PolesNumber)
                except Exception:
                    used += 1
            pt = rc.element_point(el)
            lid = rc.element_level_id(el)
            lvl = rc.eid_int(lid) if lid is not None else None
            region = region_index.locate(pt.X, pt.Y, lvl) if pt is not None else None
            locked = rc.is_owned_by_other(doc, el.Id)
            panels.append(PanelInfo(
                rc.eid_int(el.Id), name, ptype, lvl,
                rc.element_name(doc.GetElement(lid)) if lid is not None else '',
                (pt.X, pt.Y, pt.Z) if pt is not None else None, max_poles, used,
                [region or unassigned], locked))
            if not ptype:
                logger.warning('Panel "%s": no panel type (parameter or name prefix) - it will not receive circuits', name)
            if max_poles <= 0:
                logger.warning('Panel "%s": Max #1 Pole Breakers not set - treated as full', name)
            if locked:
                logger.warning('Panel "%s" is owned by another user - excluded', name)
        except Exception as ex:
            logger.warning('Panel %s skipped: %s', rc.eid_int(el.Id), ex)
    if apply_overrides:
        apply_region_overrides(panels, settings.get('region_panel_overrides', {}), logger)
    if scope_names is not None:
        panels = filter_panels_to_scope(panels, scope_names)
    logger.info('Collected %d panel(s)', len(panels))
    return panels
