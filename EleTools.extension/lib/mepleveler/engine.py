# -*- coding: utf-8 -*-
"""Core engine: element collection, analysis and transactional execution.

Execution strategy
------------------
For every element the engine:

1. takes a geometric fingerprint (:mod:`mepleveler.geometry`);
2. opens a **SubTransaction**;
3. writes the new level id;
4. regenerates and measures the displacement;
5. if the element moved vertically, shifts the offset parameter(s) by exactly
   the measured amount and regenerates again;
6. re-measures. If anything still moved - vertically, horizontally, or in
   orientation - the SubTransaction is **rolled back** and the element is
   reported as failed.

This "measure and compensate" approach is deliberately agnostic about whether
Revit recomputes the offset by itself (which it does for some categories) or
not (family instances): either way the element ends up exactly where it
started, or it is not modified at all.

Connected elements (ducts, pipes, conduits, cable trays) are optionally
fingerprinted as well, so that a transient move which drags a neighbour is
detected and rolled back too.
"""

import traceback

from System.Collections.Generic import List

from Autodesk.Revit import DB

from . import config
from . import geometry
from . import params as params_mod
from .levels import LevelResolver
from .revit_compat import (eid_value, make_eid, is_valid_id, family_and_type,
                           get_bip_param)


# ---------------------------------------------------------------------------
# Statuses
# ---------------------------------------------------------------------------

class Status(object):
    PENDING = 'Pending'            # a change is proposed
    ALREADY_CORRECT = 'Correct'    # element is already on the right level
    SKIPPED = 'Skipped'            # deliberately not processed
    UNSUPPORTED = 'Unsupported'    # no writable level parameter
    ERROR = 'Error'                # analysis failed
    UPDATED = 'Updated'            # successfully re-levelled
    FAILED = 'Failed'              # attempted and rolled back
    DESELECTED = 'Deselected'      # user unticked it in the preview


#: Statuses that represent an actionable change.
ACTIONABLE = (Status.PENDING,)


# ---------------------------------------------------------------------------
# Plan object
# ---------------------------------------------------------------------------

class ElementPlan(object):
    """Everything known about one element, before and after execution."""

    __slots__ = (
        'element_id', 'element', 'category', 'family', 'type_name',
        'elevation', 'current_level_id', 'current_level_name',
        'proposed_level_id', 'proposed_level_name',
        'current_offset', 'proposed_offset',
        'applied_offset', 'level_param_name', 'offset_param_name', 'mode',
        'status', 'message', 'selected', 'delta',
    )

    def __init__(self, element):
        self.element = element
        self.element_id = eid_value(element.Id)
        self.category = u''
        self.family = u''
        self.type_name = u''
        self.elevation = None
        self.current_level_id = None
        self.current_level_name = u''
        self.proposed_level_id = None
        self.proposed_level_name = u''
        self.current_offset = None
        self.proposed_offset = None
        self.applied_offset = None
        self.level_param_name = u''
        self.offset_param_name = u''
        self.mode = u''
        self.status = Status.PENDING
        self.message = u''
        self.selected = True
        self.delta = None  # residual movement measured after execution (ft)

    @property
    def is_actionable(self):
        return self.status in ACTIONABLE

    def __repr__(self):
        return '<ElementPlan %s %s -> %s (%s)>' % (
            self.element_id, self.current_level_name,
            self.proposed_level_name, self.status)


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------

class WarningSwallower(DB.IFailuresPreprocessor):
    """Suppresses benign warnings, rolls the transaction back on real errors.

    Warnings such as "Elements have duplicate values" are unavoidable noise
    during a bulk edit. Genuine errors must never be resolved automatically,
    so the whole batch is rolled back and the elements are reported.
    """

    def __init__(self, logger=None):
        self.logger = logger
        self.errors = []

    def PreprocessFailures(self, failures_accessor):
        try:
            failures = failures_accessor.GetFailureMessages()
            severity = failures_accessor.GetSeverity()

            if severity == DB.FailureSeverity.Warning:
                failures_accessor.DeleteAllWarnings()
                return DB.FailureProcessingResult.Continue

            if severity == DB.FailureSeverity.Error:
                for failure in failures:
                    try:
                        text = failure.GetDescriptionText()
                    except Exception:
                        text = 'Unknown Revit error'
                    self.errors.append(text)
                    if self.logger:
                        self.logger.error('Revit failure: %s', text)
                return DB.FailureProcessingResult.ProceedWithRollBack
        except Exception:
            if self.logger:
                self.logger.error('Failure preprocessor crashed:\n%s',
                                  traceback.format_exc())
        return DB.FailureProcessingResult.Continue


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class LevelFixEngine(object):
    """Analyses and corrects the level association of MEP elements."""

    def __init__(self, doc, settings, logger):
        self.doc = doc
        self.settings = settings
        self.logger = logger
        self.resolver = LevelResolver(
            doc,
            strategy=settings.strategy,
            excluded_keywords=settings.excluded_level_keywords,
            snap_tolerance=settings.snap_tolerance,
            allowed_names=settings.levels_for(doc))
        self.category_defs = config.category_map()
        self._cat_by_id = {}
        for cat_def in self.category_defs.values():
            try:
                self._cat_by_id[int(cat_def.bic)] = cat_def
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Collection
    # ------------------------------------------------------------------
    def build_category_filter(self, bic_names):
        """ElementMulticategoryFilter for the requested BuiltInCategory names."""
        bics = List[DB.BuiltInCategory]()
        for name in bic_names:
            cat_def = self.category_defs.get(name)
            if cat_def is not None:
                bics.Add(cat_def.bic)
        if bics.Count == 0:
            return None
        return DB.ElementMulticategoryFilter(bics)


    def _base_collector(self, scope, uidoc):
        """FilteredElementCollector for the requested scope, or a list of ids."""
        doc = self.doc
        if scope == config.SCOPE_SELECTION:
            ids = List[DB.ElementId](uidoc.Selection.GetElementIds())
            if ids.Count == 0:
                return None
            return DB.FilteredElementCollector(doc, ids)
        if scope == config.SCOPE_VIEW:
            view = uidoc.ActiveView
            if view is None:
                return None
            return DB.FilteredElementCollector(doc, view.Id)
        return DB.FilteredElementCollector(doc)

    def count_by_category(self, scope, uidoc, bic_names):
        """Element count per BuiltInCategory name for the given scope.

        A single pass over the model is used, which keeps the category window
        responsive even on very large federated models.
        """
        counts = dict((name, 0) for name in bic_names)
        collector = self._base_collector(scope, uidoc)
        if collector is None:
            return counts
        cat_filter = self.build_category_filter(bic_names)
        if cat_filter is None:
            return counts

        try:
            elements = collector.WherePasses(cat_filter) \
                .WhereElementIsNotElementType()
            for element in elements:
                try:
                    cat = element.Category
                    if cat is None:
                        continue
                    cat_def = self._cat_by_id.get(eid_value(cat.Id))
                    if cat_def is not None:
                        counts[cat_def.bic_name] = counts.get(
                            cat_def.bic_name, 0) + 1
                except Exception:
                    continue
        except Exception:
            self.logger.error('Category count failed:\n%s',
                              traceback.format_exc())
        return counts

    def fast_level_id(self, element):
        """Cheap read of the level an element currently references.

        Used for the level usage counts in the UI, where full parameter
        discovery for every element would be far too slow.
        """
        try:
            level_id = element.LevelId
            if is_valid_id(level_id):
                return level_id
        except Exception:
            pass
        for name in ('RBS_START_LEVEL_PARAM', 'FAMILY_LEVEL_PARAM',
                     'INSTANCE_SCHEDULE_ONLY_LEVEL_PARAM',
                     'SCHEDULE_LEVEL_PARAM', 'FAMILY_BASE_LEVEL_PARAM'):
            param = get_bip_param(element, name)
            if param is None:
                continue
            try:
                if param.StorageType != DB.StorageType.ElementId:
                    continue
                value = param.AsElementId()
                if is_valid_id(value):
                    return value
            except Exception:
                continue
        return None

    def count_by_level(self, scope, uidoc, bic_names):
        """Element count per level id for the given scope and categories."""
        counts = {}
        collector = self._base_collector(scope, uidoc)
        if collector is None:
            return counts
        cat_filter = self.build_category_filter(bic_names)
        if cat_filter is None:
            return counts
        try:
            elements = collector.WherePasses(cat_filter) \
                .WhereElementIsNotElementType()
            for element in elements:
                level_id = self.fast_level_id(element)
                if level_id is None:
                    continue
                key = eid_value(level_id)
                counts[key] = counts.get(key, 0) + 1
        except Exception:
            self.logger.error('Level usage count failed:\n%s',
                              traceback.format_exc())
        return counts

    def collect(self, scope, uidoc, bic_names):
        """Return the elements to analyse for the given scope and categories."""
        collector = self._base_collector(scope, uidoc)
        if collector is None:
            return []
        cat_filter = self.build_category_filter(bic_names)
        if cat_filter is None:
            return []

        elements = []
        try:
            query = collector.WherePasses(cat_filter) \
                .WhereElementIsNotElementType()
            for element in query:
                elements.append(element)
        except Exception:
            self.logger.error('Collection failed:\n%s', traceback.format_exc())
        self.logger.info('Collected %d candidate element(s) for scope "%s"',
                         len(elements), scope)
        return elements

    # ------------------------------------------------------------------
    # Pre-filtering
    # ------------------------------------------------------------------
    def _skip_reason(self, element):
        """Return a reason string when the element must not be processed."""
        settings = self.settings

        # View specific (annotation, detail item) elements never carry a level
        # association worth fixing.
        try:
            if element.ViewSpecific:
                return u'View specific element'
        except Exception:
            pass

        # Elements coming from a linked model cannot be modified.
        try:
            if isinstance(element, DB.RevitLinkInstance):
                return u'Linked model instance'
        except Exception:
            pass

        if settings.skip_grouped:
            try:
                if is_valid_id(element.GroupId):
                    return u'Element belongs to a group'
            except Exception:
                pass

        if settings.skip_design_option_elements:
            try:
                if element.DesignOption is not None:
                    return u'Element belongs to a design option'
            except Exception:
                pass

        if settings.skip_pinned:
            try:
                if element.Pinned:
                    return u'Element is pinned'
            except Exception:
                pass

        if settings.skip_nested_components:
            try:
                if isinstance(element, DB.FamilyInstance) \
                        and element.SuperComponent is not None:
                    return u'Nested component (follows its parent)'
            except Exception:
                pass

        # Worksharing: an element owned by somebody else cannot be edited.
        try:
            if self.doc.IsWorkshared:
                status = DB.WorksharingUtils.GetCheckoutStatus(self.doc,
                                                               element.Id)
                if status == DB.CheckoutStatus.OwnedByOtherUser:
                    owner = u''
                    try:
                        info = DB.WorksharingUtils.GetWorksharingTooltipInfo(
                            self.doc, element.Id)
                        owner = info.Owner or u''
                    except Exception:
                        pass
                    return u'Owned by another user%s' % (
                        u' (%s)' % owner if owner else u'')
        except Exception:
            pass

        return None

    # ------------------------------------------------------------------
    # Analysis (read-only, no transaction)
    # ------------------------------------------------------------------
    def analyze(self, elements, progress=None):
        """Build an :class:`ElementPlan` for every element.

        ``progress`` is an optional callable
        ``progress(index, total, category, label) -> bool``; returning False
        cancels the analysis.
        """
        plans = []
        total = len(elements)
        for index, element in enumerate(elements):
            if progress is not None:
                label = u'ID %s' % eid_value(element.Id)
                cat_name = u''
                try:
                    cat_name = element.Category.Name
                except Exception:
                    pass
                if not progress(index, total, cat_name, label):
                    self.logger.warning('Analysis cancelled by user at %d/%d',
                                        index, total)
                    break
            try:
                plans.append(self._analyze_one(element))
            except Exception:
                plan = ElementPlan(element)
                plan.status = Status.ERROR
                plan.message = u'Analysis error: %s' % traceback.format_exc(
                ).splitlines()[-1]
                plans.append(plan)
                self.logger.error('Analysis failed for %s:\n%s',
                                  eid_value(element.Id),
                                  traceback.format_exc())
        self.logger.info('Analysed %d element(s)', len(plans))
        return plans

    def _analyze_one(self, element):
        doc = self.doc
        plan = ElementPlan(element)

        try:
            plan.category = element.Category.Name
        except Exception:
            plan.category = u''
        plan.family, plan.type_name = family_and_type(doc, element)

        # -- hard skips ---------------------------------------------------
        reason = self._skip_reason(element)
        if reason:
            plan.status = Status.SKIPPED
            plan.message = reason
            plan.selected = False
            return plan

        # -- parameter discovery -----------------------------------------
        binding = params_mod.find_level_param(
            doc, element,
            allow_schedule_only=self.settings.use_schedule_level_fallback)
        plan.level_param_name = binding.level_param_name
        plan.offset_param_name = binding.offset_param_name
        plan.mode = binding.mode

        # -- current level ------------------------------------------------
        current_id = binding.current_level_id()
        if not is_valid_id(current_id):
            try:
                if is_valid_id(element.LevelId):
                    current_id = element.LevelId
            except Exception:
                current_id = None
        plan.current_level_id = eid_value(current_id) if is_valid_id(
            current_id) else None
        current_level = self.resolver.by_id(current_id)
        plan.current_level_name = current_level.name if current_level else u'<none>'
        plan.current_offset = binding.current_offset()

        if not binding.has_level:
            plan.status = Status.UNSUPPORTED
            plan.message = params_mod.read_only_reason(doc, element)
            plan.selected = False
            return plan

        # -- elevation ----------------------------------------------------
        cat_def = None
        try:
            cat_def = self._cat_by_id.get(eid_value(element.Category.Id))
        except Exception:
            cat_def = None
        rule = cat_def.elevation_rule if cat_def else config.ELEV_AUTO

        elevation = geometry.reference_elevation(
            element, self.settings.elevation_source, rule)
        if elevation is None:
            plan.status = Status.ERROR
            plan.message = u'Element has no usable geometry or location'
            plan.selected = False
            return plan
        plan.elevation = elevation

        # -- target level -------------------------------------------------
        target = self.resolver.resolve(elevation)
        if target is None:
            plan.status = Status.ERROR
            plan.message = u'No valid project level found for this elevation'
            plan.selected = False
            return plan
        plan.proposed_level_id = target.id_value
        plan.proposed_level_name = target.name

        if plan.current_level_id == target.id_value:
            plan.status = Status.ALREADY_CORRECT
            plan.message = u'Already associated with the correct level'
            plan.proposed_offset = plan.current_offset
            plan.selected = False
            return plan

        # Make it obvious in the report when the element was sitting on a
        # level the user excluded from the target list.
        off_target_note = u''
        if current_level is not None and not self.resolver.is_allowed(
                current_level):
            off_target_note = (u'Current level "%s" is not a target level. '
                               % current_level.name)
        # -- predicted offset ---------------------------------------------
        if binding.mode == params_mod.ParamBinding.MODE_SCHEDULE_ONLY:
            plan.proposed_offset = plan.current_offset
            plan.message = u'Schedule Level only (geometry untouched)'
        elif plan.current_offset is not None and current_level is not None:
            # New offset keeps the absolute elevation identical:
            #   z = level_elevation + offset  ->  offset' = offset + (e0 - e1)
            plan.proposed_offset = plan.current_offset + (
                current_level.elevation - target.elevation)
        elif plan.current_offset is not None and current_level is None:
            plan.proposed_offset = elevation - target.elevation
        else:
            plan.proposed_offset = None
            if not binding.can_compensate:
                plan.message = (u'No writable offset parameter - the level '
                                u'change will only be applied if Revit keeps '
                                u'the element in place')

        if off_target_note:
            plan.message = off_target_note + (plan.message or u'')

        plan.status = Status.PENDING
        plan.selected = True
        return plan

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------
    def execute(self, plans, progress=None):
        """Apply the selected plans inside a single, undoable transaction group.

        Returns the list of plans that were attempted, with their status and
        message updated in place.
        """
        targets = [p for p in plans if p.selected and p.is_actionable]
        if not targets:
            self.logger.info('Nothing to execute')
            return []

        self._checkout(targets)

        tolerance = float(self.settings.move_tolerance)
        batch_size = max(1, int(self.settings.batch_size))
        total = len(targets)
        cancelled = False

        group = DB.TransactionGroup(self.doc, 'Fix MEP Reference Levels')
        group.Start()
        try:
            index = 0
            while index < total and not cancelled:
                batch = targets[index:index + batch_size]
                transaction = DB.Transaction(
                    self.doc,
                    'Fix MEP Levels (%d-%d)' % (index + 1,
                                                index + len(batch)))
                handler = WarningSwallower(self.logger)
                transaction.Start()
                options = transaction.GetFailureHandlingOptions()
                options.SetFailuresPreprocessor(handler)
                options.SetClearAfterRollback(True)
                options.SetForcedModalHandling(True)
                transaction.SetFailureHandlingOptions(options)

                try:
                    for offset_in_batch, plan in enumerate(batch):
                        position = index + offset_in_batch
                        if progress is not None:
                            if not progress(position, total, plan.category,
                                            u'ID %s' % plan.element_id):
                                cancelled = True
                                break
                        self._apply_one(plan, tolerance)

                    if transaction.GetStatus() == DB.TransactionStatus.Started:
                        transaction.Commit()
                except Exception:
                    self.logger.error('Batch failed:\n%s',
                                      traceback.format_exc())
                    if transaction.GetStatus() == DB.TransactionStatus.Started:
                        transaction.RollBack()
                    for plan in batch:
                        if plan.status == Status.PENDING:
                            plan.status = Status.FAILED
                            plan.message = (u'Batch rolled back after a Revit '
                                            u'error')
                index += batch_size

            if cancelled:
                self.logger.warning('Execution cancelled - rolling back all '
                                    'committed batches')
                group.RollBack()
                for plan in targets:
                    if plan.status == Status.UPDATED:
                        plan.status = Status.FAILED
                        plan.message = u'Rolled back (user cancelled)'
            else:
                # Assimilate collapses every batch into one undo step.
                group.Assimilate()
        except Exception:
            self.logger.error('Transaction group failed:\n%s',
                              traceback.format_exc())
            try:
                group.RollBack()
            except Exception:
                pass
            raise

        updated = len([p for p in targets if p.status == Status.UPDATED])
        self.logger.info('Execution finished: %d updated, %d failed',
                         updated, len(targets) - updated)
        return targets

    def _checkout(self, plans):
        """Check out worksharing elements before the transaction opens."""
        if not self.doc.IsWorkshared:
            return
        try:
            ids = List[DB.ElementId]()
            for plan in plans:
                ids.Add(plan.element.Id)
            DB.WorksharingUtils.CheckoutElements(self.doc, ids)
            self.logger.info('Checked out %d element(s)', ids.Count)
        except Exception:
            self.logger.warning('Checkout failed (continuing):\n%s',
                                traceback.format_exc())

    # ------------------------------------------------------------------
    # Single element application
    # ------------------------------------------------------------------
    def _apply_one(self, plan, tolerance):
        """Apply one plan inside its own SubTransaction.

        The element is only kept when its fingerprint - and, optionally, the
        fingerprints of its connected neighbours - are unchanged.
        """
        doc = self.doc
        element = plan.element

        sub = DB.SubTransaction(doc)
        sub.Start()
        try:
            binding = params_mod.find_level_param(
                doc, element,
                allow_schedule_only=self.settings.use_schedule_level_fallback)
            if not binding.has_level:
                plan.status = Status.UNSUPPORTED
                plan.message = params_mod.read_only_reason(doc, element)
                sub.RollBack()
                return

            before = geometry.capture(element)
            neighbours = []
            if self.settings.verify_connected_elements:
                neighbours = self._capture_neighbours(element)

            if isinstance(plan.proposed_level_id, DB.ElementId):
                target_id = plan.proposed_level_id
            else:
                target_id = make_eid(plan.proposed_level_id)

            # 1. Write the level id.
            binding.level_param.Set(target_id)

            # Schedule-only parameters cannot move anything: commit early.
            if binding.mode == params_mod.ParamBinding.MODE_SCHEDULE_ONLY:
                sub.Commit()
                plan.status = Status.UPDATED
                plan.applied_offset = plan.current_offset
                plan.delta = 0.0
                if not plan.message:
                    plan.message = u'Schedule Level updated (geometry untouched)'
                return

            doc.Regenerate()
            after = geometry.capture(element)

            # 2. Reject any horizontal displacement outright.
            horizontal = geometry.horizontal_delta(before, after)
            if horizontal is None or horizontal > tolerance:
                sub.RollBack()
                plan.status = Status.FAILED
                plan.message = (u'Rolled back: horizontal displacement of '
                                u'%.6f ft detected' % (horizontal or -1))
                return

            # 3. Compensate any vertical displacement with the offset params.
            dz = geometry.vertical_delta(before, after)
            if dz is None:
                sub.RollBack()
                plan.status = Status.FAILED
                plan.message = u'Rolled back: geometry could not be verified'
                return

            if abs(dz) > tolerance:
                if not binding.offset_params:
                    sub.RollBack()
                    plan.status = Status.FAILED
                    plan.message = (u'Rolled back: element moved %.6f ft and '
                                    u'no writable offset parameter exists'
                                    % dz)
                    return
                for param in binding.offset_params:
                    try:
                        param.Set(param.AsDouble() + dz)
                    except Exception:
                        sub.RollBack()
                        plan.status = Status.FAILED
                        plan.message = (u'Rolled back: offset parameter "%s" '
                                        u'could not be written'
                                        % params_mod.param_name(param))
                        return
                doc.Regenerate()
                after = geometry.capture(element)

            # 4. Final verification of the element itself.
            residual = geometry.max_delta(before, after)
            if residual is None or residual > tolerance:
                sub.RollBack()
                plan.status = Status.FAILED
                plan.message = (u'Rolled back: residual displacement of '
                                u'%.6f ft after compensation'
                                % (residual if residual is not None else -1))
                return

            if geometry.orientation_changed(before, after):
                sub.RollBack()
                plan.status = Status.FAILED
                plan.message = u'Rolled back: element orientation changed'
                return

            # 5. Verification of connected neighbours (system integrity).
            moved_neighbour = self._verify_neighbours(neighbours, tolerance)
            if moved_neighbour is not None:
                sub.RollBack()
                plan.status = Status.FAILED
                plan.message = (u'Rolled back: connected element %s moved '
                                u'by %.6f ft' % moved_neighbour)
                return

            sub.Commit()
            plan.status = Status.UPDATED
            plan.delta = residual
            plan.applied_offset = binding.current_offset()
            if plan.applied_offset is not None:
                plan.proposed_offset = plan.applied_offset
            if not plan.message:
                plan.message = u'Level corrected, position preserved'

        except Exception:
            try:
                if sub.GetStatus() == DB.TransactionStatus.Started:
                    sub.RollBack()
            except Exception:
                pass
            plan.status = Status.FAILED
            plan.message = u'Exception: %s' % traceback.format_exc(
            ).splitlines()[-1]
            self.logger.error('Apply failed for %s:\n%s', plan.element_id,
                              traceback.format_exc())

    # ------------------------------------------------------------------
    # Connectivity verification
    # ------------------------------------------------------------------
    def _connector_manager(self, element):
        try:
            if isinstance(element, DB.MEPCurve):
                return element.ConnectorManager
            if isinstance(element, DB.FamilyInstance):
                mep_model = element.MEPModel
                if mep_model is not None:
                    return mep_model.ConnectorManager
        except Exception:
            pass
        return None

    def _capture_neighbours(self, element):
        """Fingerprint every element physically connected to *element*."""
        captured = []
        manager = self._connector_manager(element)
        if manager is None:
            return captured
        seen = set()
        try:
            for connector in manager.Connectors:
                try:
                    if not connector.IsConnected:
                        continue
                    for other in connector.AllRefs:
                        owner = other.Owner
                        if owner is None:
                            continue
                        owner_id = eid_value(owner.Id)
                        if owner_id == eid_value(element.Id):
                            continue
                        if owner_id in seen:
                            continue
                        seen.add(owner_id)
                        captured.append((owner_id, owner,
                                         geometry.capture(owner)))
                except Exception:
                    continue
        except Exception:
            pass
        return captured

    def _verify_neighbours(self, captured, tolerance):
        """Return (id, delta) of the first neighbour that moved, else None."""
        for owner_id, owner, before in captured:
            try:
                after = geometry.capture(owner)
                delta = geometry.max_delta(before, after)
                if delta is None or delta > tolerance:
                    return (owner_id, delta if delta is not None else -1)
            except Exception:
                continue
        return None


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def summarize(plans):
    """Return a dict of summary statistics for a list of plans."""
    stats = {
        'scanned': len(plans),
        'updated': 0,
        'skipped': 0,
        'failed': 0,
        'already_correct': 0,
        'unsupported': 0,
        'errors': 0,
        'pending': 0,
        'deselected': 0,
    }
    for plan in plans:
        if plan.status == Status.UPDATED:
            stats['updated'] += 1
        elif plan.status == Status.FAILED:
            stats['failed'] += 1
        elif plan.status == Status.SKIPPED:
            stats['skipped'] += 1
        elif plan.status == Status.ALREADY_CORRECT:
            stats['already_correct'] += 1
        elif plan.status == Status.UNSUPPORTED:
            stats['unsupported'] += 1
        elif plan.status == Status.ERROR:
            stats['errors'] += 1
        elif plan.status == Status.DESELECTED:
            stats['deselected'] += 1
        elif plan.status == Status.PENDING:
            stats['pending'] += 1
    # "Skipped" in the user facing summary covers everything not updated and
    # not failed.
    stats['total_skipped'] = (stats['skipped'] + stats['already_correct'] +
                              stats['unsupported'] + stats['deselected'] +
                              stats['pending'])
    return stats
