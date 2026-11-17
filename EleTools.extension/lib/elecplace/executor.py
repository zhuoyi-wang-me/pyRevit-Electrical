# -*- coding: utf-8 -*-
"""
elecplace.executor
==================
Creates the planned fixtures inside the model: on linked walls, on
(linked or host) equipment faces, or level-based at the planned point.

Transaction strategy
  TransactionGroup "Auto-Place Electrical Fixtures"   (one Undo item)
    Transaction "Activate fixture types"
    Transaction "Place fixtures - <room>"              (one per room)
      SubTransaction per fixture                        (isolates failures)
  -> TransactionGroup.Assimilate()

A failing fixture only rolls back its SubTransaction; a failing room only
rolls back its Transaction; any unexpected error rolls back the group.
Warnings are swallowed by an IFailuresPreprocessor so the user is not
flooded with dialogs; errors still roll back normally.

Revit API used:
  DB.TransactionGroup, DB.Transaction, DB.SubTransaction,
  DB.IFailuresPreprocessor, DB.FailuresAccessor, DB.FailureSeverity,
  DB.FailureProcessingResult, DB.TransactionStatus,
  DB.FamilySymbol.Activate, Document.Create.NewFamilyInstance
  (Reference, XYZ, XYZ, FamilySymbol) and (XYZ, FamilySymbol, Level,
  StructuralType), DB.ElementTransformUtils.RotateElement,
  DB.FamilyInstance.FacingOrientation / flipFacing, DB.XYZ.AngleOnPlaneTo
"""
from collections import OrderedDict

from Autodesk.Revit import DB
from Autodesk.Revit.DB.Structure import StructuralType

from elecplace.compat import to_text
from elecplace.planner import MODE_FACE, MODE_UNHOSTED


class WarningSwallower(DB.IFailuresPreprocessor):
    """Delete warnings (e.g. 'instance slightly off axis'); keep errors."""

    def PreprocessFailures(self, failures_accessor):
        try:
            for msg in failures_accessor.GetFailureMessages():
                if msg.GetSeverity() == DB.FailureSeverity.Warning:
                    failures_accessor.DeleteWarning(msg)
        except Exception:
            pass
        return DB.FailureProcessingResult.Continue


class ExecResult(object):
    def __init__(self):
        self.placed = []     # [(Planned, ElementId)]
        self.errors = []     # [(Planned or None, message)]
        self.flipped = 0
        self.data_written = {}   # {parameter: fixtures written}
        self.data_failed = {}    # {(parameter, reason): fixtures}


def _set_param(element, bip, value):
    try:
        prm = element.get_Parameter(bip)
        if prm is not None and not prm.IsReadOnly:
            prm.Set(value)
            return True
    except Exception:
        pass
    return False


def _write_data(inst, pp, result):
    """Write the rule's resolved parameter values (after the Comments tag)."""
    from elecplace.datamap import write_param
    for name, text in pp.param_values:
        err = write_param(inst, name, text)
        if err:
            key = (name, err)
            result.data_failed[key] = result.data_failed.get(key, 0) + 1
        else:
            result.data_written[name] = result.data_written.get(name, 0) + 1


def _place_one(doc, pp, settings):
    """Create one family instance according to its planned mode."""
    symbol = pp.rule.symbol
    if not symbol.IsValidObject:
        raise Exception("Family type is no longer loaded")

    if pp.rule.mode == MODE_FACE:
        # Face-based hosting on the LINKED wall face (host coordinates).
        inst = doc.Create.NewFamilyInstance(pp.link_ref, pp.point_host,
                                            pp.ref_dir_host, symbol)
    elif pp.rule.mode == MODE_UNHOSTED:
        lvl = pp.host_level
        if lvl is None:
            raise Exception("No host level available")
        base = DB.XYZ(pp.point_host.X, pp.point_host.Y, lvl.ProjectElevation)
        inst = doc.Create.NewFamilyInstance(base, symbol, lvl, StructuralType.NonStructural)
        _set_param(inst, DB.BuiltInParameter.INSTANCE_ELEVATION_PARAM, pp.level_offset)
        # Rotate so the family's facing direction points into the room.
        doc.Regenerate()
        facing = inst.FacingOrientation
        # rotate_dir is set for on-equipment placements (also valid for
        # top / bottom faces whose normal is vertical).
        src = pp.rotate_dir or pp.normal_host
        target = DB.XYZ(src.X, src.Y, 0.0).Normalize()
        angle = facing.AngleOnPlaneTo(target, DB.XYZ.BasisZ)
        if abs(angle) > 1e-6:
            axis = DB.Line.CreateBound(base, base.Add(DB.XYZ.BasisZ))
            DB.ElementTransformUtils.RotateElement(doc, inst.Id, axis, angle)
    else:
        raise Exception("Rule has no valid placement mode")

    # Traceability: tag + room written into Comments - only when switched on
    # (Options tab "Write placement info into Comments"; off by default so the
    # tool never fills a parameter the user did not ask for).
    if getattr(settings, "write_comments", False):
        comment = u"{} | Room {} | {}".format(settings.comment_tag, pp.room.label,
                                              pp.room.link.title)
        if pp.served:
            comment += u" | Serves " + pp.served
        _set_param(inst, DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS, comment)

    # Face-based instances get a schedule level automatically; align it with
    # the host level matching the linked room level when allowed.
    if settings.assign_schedule_level and pp.rule.mode == MODE_FACE and pp.host_level:
        _set_param(inst, DB.BuiltInParameter.INSTANCE_SCHEDULE_ONLY_LEVEL_PARAM,
                   pp.host_level.Id)
    return inst


def _verify_facing(doc, created, result):
    """After regeneration, flip any instance that faces away from its room."""
    doc.Regenerate()
    for pp, inst in created:
        try:
            if inst.FacingOrientation.DotProduct(pp.normal_host) < -0.5 and inst.CanFlipFacing:
                inst.flipFacing()
                result.flipped += 1
        except Exception:
            pass


def execute(doc, plan, settings, logger=None):
    result = ExecResult()
    if not plan.placements:
        return result

    tgroup = DB.TransactionGroup(doc, "Auto-Place Electrical Fixtures")
    tgroup.Start()
    try:
        # ---- 1. Activate symbols (required before NewFamilyInstance) ------
        symbols = OrderedDict((pp.rule.symbol_id, pp.rule.symbol) for pp in plan.placements)
        t = DB.Transaction(doc, "Activate fixture types")
        t.Start()
        for sym in symbols.values():
            if not sym.IsActive:
                sym.Activate()
        doc.Regenerate()
        t.Commit()

        # ---- 2. One transaction per room ----------------------------------
        by_room = OrderedDict()
        for pp in plan.placements:
            by_room.setdefault(pp.room.uid, []).append(pp)

        for items in by_room.values():
            room = items[0].room
            t = DB.Transaction(doc, u"Place fixtures - Room {}".format(room.label))
            opts = t.GetFailureHandlingOptions()
            opts.SetFailuresPreprocessor(WarningSwallower())
            opts.SetClearAfterRollback(True)
            t.SetFailureHandlingOptions(opts)
            t.Start()
            created = []
            try:
                for pp in items:
                    st = DB.SubTransaction(doc)
                    st.Start()
                    try:
                        inst = _place_one(doc, pp, settings)
                        if pp.param_values:
                            _write_data(inst, pp, result)   # a failed value never undoes the fixture
                        st.Commit()
                        created.append((pp, inst))
                    except Exception as ex:
                        if st.HasStarted() and not st.HasEnded():
                            st.RollBack()
                        result.errors.append((pp, to_text(ex)))
                        if logger:
                            logger.debug(u"Placement failed in {}: {}".format(room.label, ex))
                if created:
                    _verify_facing(doc, created, result)
                status = t.Commit()
            except Exception as ex:
                if t.HasStarted() and not t.HasEnded():
                    t.RollBack()
                for pp, _inst in created:
                    result.errors.append((pp, u"Room transaction rolled back: " + to_text(ex)))
                continue

            if status == DB.TransactionStatus.Committed:
                result.placed.extend((pp, inst.Id) for pp, inst in created)
            else:
                for pp, _inst in created:
                    result.errors.append((pp, u"Room transaction status: {}".format(status)))

        tgroup.Assimilate()
    except Exception:
        if tgroup.HasStarted() and not tgroup.HasEnded():
            tgroup.RollBack()
        raise
    return result
