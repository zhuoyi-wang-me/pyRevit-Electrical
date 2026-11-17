# -*- coding: utf-8 -*-
"""
Creates ElectricalSystems (circuits) from a CircuitPlan.

Transaction model
-----------------
TransactionGroup "ElecAutoCircuit <run>"           -> a single Undo entry for the whole run
  Transaction    "AutoCircuit: N circuit(s)"        -> batches of N (fast path)
     batch fails -> it is rolled back and replayed one circuit per transaction,
                    so one bad element never sinks the rest of the batch.
settings.general.all_or_nothing = true -> any failure rolls back the whole group.

Revit warnings raised during commit are captured (and suppressed) by a failures
preprocessor; errors force ProceedWithRollBack so no modal dialog interrupts a
large run.
"""
import clr
clr.AddReference('RevitAPI')
from Autodesk.Revit.DB import (Transaction, TransactionGroup, TransactionStatus, IFailuresPreprocessor,
                               FailureProcessingResult, FailureSeverity, BuiltInParameter)

from autocircuit import revit_compat as rc
from autocircuit.models import STATUS_PLANNED, STATUS_CREATED, STATUS_FAILED


class FailureCollector(IFailuresPreprocessor):
    __namespace__ = 'ElecAutoCircuit'   # required by pythonnet (CPython engine), ignored by IronPython

    def __init__(self):
        self.messages = []

    def PreprocessFailures(self, accessor):
        has_error = False
        for msg in accessor.GetFailureMessages():
            text = msg.GetDescriptionText()
            if msg.GetSeverity() == FailureSeverity.Warning:
                self.messages.append(('Warning', text))
                accessor.DeleteWarning(msg)
            else:
                self.messages.append(('Error', text))
                has_error = True
        return FailureProcessingResult.ProceedWithRollBack if has_error else FailureProcessingResult.Continue


def _clean(ex):
    msg = getattr(ex, 'Message', None) or str(ex)
    return msg.strip().splitlines()[0] if msg else ex.__class__.__name__


class CircuitBuilder(object):

    def __init__(self, doc, settings, logger):
        g = settings['general']
        self.doc = doc
        self.log = logger
        self.batch = max(1, int(g.get('transaction_batch_size', 50) or 1))
        self.all_or_nothing = bool(g.get('all_or_nothing', False))
        self.tag = bool(g.get('tag_circuits', True))
        self.template = g.get('load_name_template') or ''
        self.run_id = ''

    # ------------------------------------------------------------------ run
    def execute(self, plan):
        self.run_id = plan.run_id
        todo = [c for c in plan.circuits if c.status == STATUS_PLANNED]
        if not todo:
            plan.warn('Nothing to create - no circuit could be planned.')
            return plan

        tg = TransactionGroup(self.doc, 'ElecAutoCircuit {0}'.format(plan.run_id))
        tg.Start()
        try:
            for i in range(0, len(todo), self.batch):
                batch = todo[i:i + self.batch]
                if not self._run(batch, plan, isolate=len(batch) == 1) and len(batch) > 1:
                    self.log.info('Batch %d failed - replaying circuit by circuit', i // self.batch + 1)
                    for c in batch:
                        c.status, c.message = STATUS_PLANNED, ''
                        self._run([c], plan, isolate=True)

            created = [c for c in todo if c.status == STATUS_CREATED]
            failed = [c for c in todo if c.status == STATUS_FAILED]
            if not created or (failed and self.all_or_nothing):
                tg.RollBack()
                for c in created:
                    c.status, c.message = STATUS_FAILED, 'Rolled back (all-or-nothing mode)'
                if failed:
                    plan.error('{0} circuit(s) failed - run rolled back.'.format(len(failed)))
            else:
                tg.Assimilate()
                self.log.info('Run %s: %d created, %d failed', plan.run_id, len(created), len(failed))
        except Exception as ex:
            self.log.exception('Fatal error during circuit creation - rolling back')
            if tg.HasStarted() and not tg.HasEnded():
                tg.RollBack()
            for c in todo:
                if c.status == STATUS_CREATED:
                    c.status, c.message = STATUS_FAILED, 'Rolled back after fatal error'
            plan.error('Fatal error, all changes rolled back: {0}'.format(_clean(ex)))
        return plan

    def _run(self, batch, plan, isolate):
        t = Transaction(self.doc, 'AutoCircuit: {0} circuit(s)'.format(len(batch)))
        collector = FailureCollector()
        opts = t.GetFailureHandlingOptions()
        opts.SetFailuresPreprocessor(collector)
        opts.SetClearAfterRollback(True)
        t.SetFailureHandlingOptions(opts)

        made = []
        t.Start()
        try:
            for c in batch:
                made.append((c, self._create(c)))
            status = t.Commit()
        except Exception as ex:
            if t.GetStatus() == TransactionStatus.Started:
                t.RollBack()
            if isolate:
                c = batch[0]
                c.status, c.message = STATUS_FAILED, _clean(ex)
                plan.error('Circuit #{0} ({1}): {2}'.format(c.uid, ', '.join(c.room_names), c.message))
            return False

        if status != TransactionStatus.Committed:
            if isolate:
                errs = [m for s, m in collector.messages if s == 'Error']
                c = batch[0]
                c.status, c.message = STATUS_FAILED, '; '.join(errs) or 'Transaction rolled back by Revit'
                plan.error('Circuit #{0} ({1}): {2}'.format(c.uid, ', '.join(c.room_names), c.message))
            return False

        for sev, msg in collector.messages:
            if sev == 'Warning':
                plan.warn('Revit: ' + msg)
        for c, system in made:
            c.status = STATUS_CREATED
            c.system_id = rc.eid_int(system.Id)
            try:
                c.circuit_number = system.CircuitNumber or ''
            except Exception:
                pass
        return True

    # --------------------------------------------------------------- circuit
    def _create(self, c):
        ids = [rc.to_eid(f.element_id) for f in c.fixtures]
        system = rc.create_power_system(self.doc, ids)
        if system is None:
            raise Exception('ElectricalSystem.Create returned no system')
        self._select_panel(system, c)
        self._stamp(system, c)
        return system

    def _select_panel(self, system, c):
        """Try the planned panel, then alternates: compatibility (distribution system,
        voltage, poles) can only be verified by Revit itself."""
        tried = []
        for panel in [c.panel] + [p for p in c.alternates if p is not c.panel]:
            if panel is not c.panel and panel.free_poles < c.poles:
                continue
            el = self.doc.GetElement(rc.to_eid(panel.element_id))
            try:
                system.SelectPanel(el)
                be = system.BaseEquipment
                if be is not None and rc.eid_int(be.Id) == panel.element_id:
                    if panel is not c.panel:
                        c.panel.release(c.poles, c.load_va)
                        panel.reserve(c.poles, c.load_va)
                        c.message = (c.message + ' ' if c.message else '') + \
                            'Panel {0} rejected the circuit; assigned to {1}.'.format(c.panel.name, panel.name)
                        c.panel = panel
                    return
                tried.append('{0} (not accepted)'.format(panel.name))
            except Exception as ex:
                tried.append('{0} ({1})'.format(panel.name, _clean(ex)))
        raise Exception('No compatible panel accepted the circuit: ' + '; '.join(tried))

    def _stamp(self, system, c):
        if self.template:
            try:
                name = self.template.format(rule=c.rule_name, rooms=', '.join(c.room_names)[:120],
                                            panel_type=c.panel_type, region=c.region, count=c.count)
            except (KeyError, IndexError, ValueError):
                name = None
            p = system.get_Parameter(BuiltInParameter.RBS_ELEC_CIRCUIT_NAME)
            if name and p is not None and not p.IsReadOnly:
                p.Set(name)
        if self.tag:
            p = system.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
            if p is not None and not p.IsReadOnly:
                p.Set('AutoCircuit {0}'.format(self.run_id))
