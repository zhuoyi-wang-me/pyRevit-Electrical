# -*- coding: utf-8 -*-
"""Entry point used by the pushbutton.

Workflow
--------
1.  Load persisted settings and open the main dialog.
2.  Collect the elements for the chosen scope and categories.
3.  Analyse them (read-only, no transaction is opened).
4.  Optionally show the preview dialog so the user can curate the change set.
5.  Execute inside one undoable transaction group, verifying after every
    element that nothing moved.
6.  Write the CSV report and print a summary to the pyRevit output window.
"""

import os
import traceback

from pyrevit import forms
from pyrevit import script

from . import config
from . import log
from . import reporting
from .engine import LevelFixEngine, summarize
from .ui.main_window import MainWindow, MainWindowResult
from .ui.preview_window import PreviewWindow
from .ui.progress import ProgressReporter


def run(doc, uidoc):
    """Run the tool. ``doc``/``uidoc`` come from ``pyrevit.revit``."""
    output = script.get_output()

    # -- guard rails ------------------------------------------------------
    if doc is None:
        forms.alert('No active Revit document.', title='MEP Level Fixer')
        return
    if doc.IsFamilyDocument:
        forms.alert('This tool runs on project documents only, not inside the '
                    'family editor.', title='MEP Level Fixer')
        return
    if doc.IsLinked:
        forms.alert('Linked documents cannot be modified.',
                    title='MEP Level Fixer')
        return

    settings = config.Settings.load()
    logger = log.get_logger(settings.debug)
    log.purge_old_logs()
    logger.info('=' * 70)
    logger.info('MEP Level Fixer started on "%s"', doc.Title)

    try:
        engine = LevelFixEngine(doc, settings, logger)
    except Exception:
        logger.error('Engine init failed:\n%s', traceback.format_exc())
        forms.alert('The tool could not start:\n\n%s' % traceback.format_exc(),
                    title='MEP Level Fixer')
        return

    if not engine.resolver.all_levels:
        forms.alert('This model contains no levels, so no element can be '
                    're-levelled.', title='MEP Level Fixer')
        return

    # -- 1. main dialog ---------------------------------------------------
    window = MainWindow(doc, uidoc, settings, engine, logger)
    window.ShowDialog()
    result = window.result
    if result.cancelled:
        logger.info('Cancelled in the main dialog')
        return

    settings = result.settings
    logger = log.get_logger(settings.debug)
    logger.info('Scope=%s  Strategy=%s  Elevation=%s  Categories=%d',
                settings.scope, settings.strategy, settings.elevation_source,
                len(settings.categories))
    logger.info('Target levels: %s',
                ', '.join(settings.levels_for(doc)) or '<all>')

    # The resolver depends on the strategy, the target levels and the level
    # exclusions, so rebuild it with the settings the user just confirmed.
    engine = LevelFixEngine(doc, settings, logger)

    if not engine.resolver.levels:
        forms.alert('None of the levels you ticked could be matched in this '
                    'model, so there is nothing to allocate elements to.\n\n'
                    'Re-open the tool and tick the target levels again.',
                    title='MEP Level Fixer')
        return

    # -- 2. collection ----------------------------------------------------
    elements = engine.collect(settings.scope, uidoc, settings.categories)
    if not elements:
        forms.alert('No elements of the selected categories were found in the '
                    'chosen scope.', title='MEP Level Fixer')
        return

    # -- 3. analysis ------------------------------------------------------
    with ProgressReporter('Analysing elements') as reporter:
        plans = engine.analyze(elements, progress=reporter.callback)
        cancelled = reporter.cancelled
    if cancelled:
        forms.alert('Analysis cancelled. Nothing was modified.',
                    title='MEP Level Fixer')
        return

    stats = summarize(plans)
    actionable = [p for p in plans if p.is_actionable]
    if not actionable:
        _report_analysis_only(output, doc, plans, stats, settings, logger)
        return

    # -- 4. preview / confirmation ---------------------------------------
    if result.action == MainWindowResult.PREVIEW:
        preview = PreviewWindow(doc, uidoc, plans, settings, logger)
        preview.ShowDialog()
        if not preview.confirmed:
            logger.info('Cancelled in the preview dialog')
            _maybe_export_preview(doc, plans, settings, logger)
            return
    else:
        message = ('%d element(s) will be re-associated with a different '
                   'level.\n\n'
                   'Offsets are recalculated so that every element stays '
                   'exactly where it is. Any element that would move is '
                   'rolled back automatically.\n\nProceed?'
                   % len(actionable))
        if not forms.alert(message, title='Confirm', yes=True, no=True):
            logger.info('Cancelled at the confirmation dialog')
            return

    # -- 5. execution -----------------------------------------------------
    try:
        with ProgressReporter('Updating levels') as reporter:
            engine.execute(plans, progress=reporter.callback)
    except Exception:
        logger.error('Execution failed:\n%s', traceback.format_exc())
        forms.alert('Execution failed and the model was rolled back:\n\n%s'
                    % traceback.format_exc().splitlines()[-1],
                    title='MEP Level Fixer')
        return

    # -- 6. reporting -----------------------------------------------------
    stats = summarize(plans)
    report_path = None
    try:
        report_path = reporting.write_csv(
            doc, plans,
            extra_info=_report_context(settings, engine))
        settings.last_report_folder = os.path.dirname(report_path)
        settings.save()
    except Exception:
        logger.error('Report generation failed:\n%s', traceback.format_exc())

    reporting.print_summary(output, doc, plans, stats,
                            logfile=log.logfile_path(),
                            report_path=report_path)
    logger.info('Finished: %s', stats)

    _offer_to_open(report_path)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _report_context(settings, engine):
    """Header rows describing how the run was configured."""
    return [
        ('Scope', config.SCOPE_LABELS.get(settings.scope, settings.scope)),
        ('Level strategy', config.STRATEGY_LABELS.get(settings.strategy,
                                                      settings.strategy)),
        ('Elevation source', config.ELEV_LABELS.get(settings.elevation_source,
                                                    settings.elevation_source)),
        ('Movement tolerance (ft)', '%.6f' % settings.move_tolerance),
        ('Categories', '; '.join(settings.categories)),
        ('Target levels', '; '.join('%s' % lv.name for lv in
                                    engine.resolver.levels)),
        ('Levels ignored', '; '.join('%s' % lv.name for lv in
                                     engine.resolver.excluded_levels)
         or 'none'),
    ]


def _report_analysis_only(output, doc, plans, stats, settings, logger):
    """Nothing to change - still give the user the full picture."""
    logger.info('No actionable changes found')
    output.print_md('# MEP Level Fixer')
    output.print_md('No element needed a level correction.')
    output.print_md(
        '| Metric | Count |\n|---|---:|\n'
        '| Scanned | {scanned} |\n'
        '| Already correct | {already_correct} |\n'
        '| Skipped by filter | {skipped} |\n'
        '| Unsupported | {unsupported} |\n'
        '| Analysis errors | {errors} |'.format(**stats))
    forms.alert('All %d scanned element(s) are already on the correct level '
                '(or were skipped).\n\nSee the pyRevit output window for the '
                'breakdown.' % stats['scanned'], title='MEP Level Fixer')


def _maybe_export_preview(doc, plans, settings, logger):
    """Offer a CSV of the preview when the user backs out."""
    if not forms.alert('No changes were applied.\n\nExport the preview to a '
                       'CSV file?', title='MEP Level Fixer',
                       yes=True, no=True):
        return
    try:
        path = reporting.write_csv(
            doc, plans,
            path=reporting.default_report_path(doc, prefix='MEPLevelPreview'),
            title='MEP Level Fixer - PREVIEW (no changes applied)',
            extra_info=[('Report type', 'Preview')])
        logger.info('Preview exported to %s', path)
        _offer_to_open(path)
    except Exception:
        logger.error('Preview export failed:\n%s', traceback.format_exc())


def _offer_to_open(path):
    if not path or not os.path.isfile(path):
        return
    try:
        if forms.alert('Report written to:\n%s\n\nOpen it now?' % path,
                       title='MEP Level Fixer', yes=True, no=True):
            os.startfile(path)
    except Exception:
        pass
