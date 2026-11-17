# -*- coding: utf-8 -*-
"""Reporting framework.

Produces:

* a CSV file that opens cleanly in Excel (UTF-8 BOM, quoted fields, CRLF);
* a summary block appended to the same file;
* an HTML summary printed to the pyRevit output window.

Offsets are written twice - once in the project display units for humans, once
in internal decimal feet - so the report can be used both for coordination
meetings and for forensic verification.
"""

import os
import codecs
from datetime import datetime

from . import log
from .engine import Status, summarize
from .revit_compat import format_length, to_display_length, length_unit_label
import safetable  # crash-safe output tables (lib/safetable.py)


HEADERS = [
    'Element ID',
    'Category',
    'Family Name',
    'Type Name',
    'Old Reference Level',
    'New Reference Level',
    'Old Offset',
    'New Offset',
    'Old Offset (ft)',
    'New Offset (ft)',
    'Element Elevation (ft)',
    'Level Parameter',
    'Offset Parameter',
    'Update Mode',
    'Residual Movement (ft)',
    'Status',
    'Message',
]


def _csv_escape(value):
    """Quote a value for CSV output."""
    if value is None:
        text = u''
    else:
        try:
            text = u'%s' % value
        except Exception:
            text = u''
    text = text.replace(u'"', u'""')
    text = text.replace(u'\r\n', u' ').replace(u'\n', u' ').replace(u'\r', u' ')
    return u'"%s"' % text


def _row(doc, plan):
    """Build one CSV row from an ElementPlan."""
    return [
        plan.element_id,
        plan.category,
        plan.family,
        plan.type_name,
        plan.current_level_name,
        plan.proposed_level_name,
        format_length(doc, plan.current_offset, 1)
        if plan.current_offset is not None else u'',
        format_length(doc, plan.proposed_offset, 1)
        if plan.proposed_offset is not None else u'',
        u'%.6f' % plan.current_offset if plan.current_offset is not None else u'',
        u'%.6f' % plan.proposed_offset if plan.proposed_offset is not None else u'',
        u'%.6f' % plan.elevation if plan.elevation is not None else u'',
        plan.level_param_name,
        plan.offset_param_name,
        plan.mode,
        u'%.8f' % plan.delta if plan.delta is not None else u'',
        plan.status,
        plan.message,
    ]


def default_report_path(doc, prefix='MEPLevelFixer', folder=None):
    """Build a timestamped report path inside the reports folder."""
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    try:
        model = os.path.splitext(os.path.basename(doc.Title))[0]
    except Exception:
        model = 'model'
    safe_model = u''.join(ch for ch in model
                          if ch.isalnum() or ch in (u' ', u'_', u'-')).strip()
    name = u'%s_%s_%s.csv' % (prefix, safe_model or u'model', stamp)
    return os.path.join(folder or log.reports_dir(), name)


def write_csv(doc, plans, path=None, title='MEP Level Fixer Report',
              extra_info=None):
    """Write the full report and return the file path.

    ``extra_info`` is an optional list of (label, value) pairs written into the
    header block (scope, categories, strategy, ...).
    """
    path = path or default_report_path(doc)
    folder = os.path.dirname(path)
    if folder and not os.path.isdir(folder):
        try:
            os.makedirs(folder)
        except Exception:
            pass

    stats = summarize(plans)
    unit = length_unit_label(doc)

    lines = []
    lines.append(_csv_escape(title))
    lines.append(_csv_escape('Generated') + ',' +
                 _csv_escape(datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
    try:
        lines.append(_csv_escape('Model') + ',' + _csv_escape(doc.Title))
        lines.append(_csv_escape('Path') + ',' + _csv_escape(doc.PathName))
    except Exception:
        pass
    lines.append(_csv_escape('Display units') + ',' + _csv_escape(unit))
    for label, value in (extra_info or []):
        lines.append(_csv_escape(label) + ',' + _csv_escape(value))
    lines.append(u'')

    # -- summary block ----------------------------------------------------
    lines.append(_csv_escape('SUMMARY'))
    lines.append(_csv_escape('Total Elements Scanned') + ',' +
                 _csv_escape(stats['scanned']))
    lines.append(_csv_escape('Total Updated') + ',' +
                 _csv_escape(stats['updated']))
    lines.append(_csv_escape('Total Skipped') + ',' +
                 _csv_escape(stats['total_skipped']))
    lines.append(_csv_escape('Total Failed') + ',' +
                 _csv_escape(stats['failed']))
    lines.append(_csv_escape('  Already Correct') + ',' +
                 _csv_escape(stats['already_correct']))
    lines.append(_csv_escape('  Unsupported') + ',' +
                 _csv_escape(stats['unsupported']))
    lines.append(_csv_escape('  Skipped by filter') + ',' +
                 _csv_escape(stats['skipped']))
    lines.append(_csv_escape('  Analysis errors') + ',' +
                 _csv_escape(stats['errors']))
    lines.append(u'')

    # -- detail table -----------------------------------------------------
    lines.append(u','.join(_csv_escape(h) for h in HEADERS))
    for plan in plans:
        lines.append(u','.join(_csv_escape(v) for v in _row(doc, plan)))

    content = u'\r\n'.join(lines) + u'\r\n'
    with codecs.open(path, 'w', encoding='utf-8-sig') as handle:
        handle.write(content)
    return path


def print_summary(output, doc, plans, stats=None, logfile=None,
                  report_path=None, max_rows=200):
    """Render an HTML summary in the pyRevit output window."""
    stats = stats or summarize(plans)

    output.print_md('# MEP Level Fixer - Results')
    output.print_md(
        '| Metric | Count |\n|---|---:|\n'
        '| Total elements scanned | {scanned} |\n'
        '| Total updated | {updated} |\n'
        '| Total skipped | {total_skipped} |\n'
        '| Total failed | {failed} |\n'
        '| - already correct | {already_correct} |\n'
        '| - unsupported | {unsupported} |\n'
        '| - filtered out | {skipped} |\n'
        '| - analysis errors | {errors} |'.format(**stats))

    changed = [p for p in plans if p.status in (Status.UPDATED, Status.FAILED)]
    if changed:
        rows = []
        for plan in changed[:max_rows]:
            rows.append([
                output.linkify(plan.element.Id) if plan.element else
                plan.element_id,
                plan.category,
                plan.type_name,
                plan.current_level_name,
                plan.proposed_level_name,
                format_length(doc, plan.current_offset, 1)
                if plan.current_offset is not None else '',
                format_length(doc, plan.proposed_offset, 1)
                if plan.proposed_offset is not None else '',
                plan.status,
                plan.message,
            ])
        safetable.print_table(output, 
            table_data=rows,
            title='Processed elements',
            columns=['ID', 'Category', 'Type', 'Old level', 'New level',
                     'Old offset', 'New offset', 'Status', 'Message'])
        if len(changed) > max_rows:
            output.print_md('_...and %d more rows - see the CSV report._'
                            % (len(changed) - max_rows))

    problems = [p for p in plans
                if p.status in (Status.FAILED, Status.ERROR,
                                Status.UNSUPPORTED)]
    if problems:
        output.print_md('## Issues (%d)' % len(problems))
        buckets = {}
        for plan in problems:
            buckets.setdefault(plan.message or plan.status, []).append(plan)
        rows = [[message, len(items)] for message, items
                in sorted(buckets.items(), key=lambda kv: -len(kv[1]))]
        safetable.print_table(output, table_data=rows, title='Issue breakdown',
                           columns=['Message', 'Count'])

    if report_path:
        output.print_md('**CSV report:** `%s`' % report_path)
    if logfile:
        output.print_md('**Log file:** `%s`' % logfile)
