# -*- coding: utf-8 -*-
"""Cable Tray QA/QC - Excel versus Revit comparison (pure Python, no Revit API).

Input
    excel        excel_reader.ExcelData
    annotations  list of dicts produced by revit_reader (one per annotation):
                   text, kind, annotation_id, element_id, view_id, view_name,
                   sheet_id, sheet_number, sheet_name, other_sheets,
                   model_width, model_height, model_tag

Output
    QAResult with .rows (one per annotation + one per missing Excel tag)
    and .summary (the counts shown in the window and the report).

Sets and dictionaries are used throughout, so the cost is linear in the number
of Excel rows plus the number of annotations.
"""
import config
from annotation_parser import normalize_tag, parse_annotation

NA = config.NOT_AVAILABLE


class QAResult(object):
    def __init__(self):
        self.rows = []
        self.summary = {}
        self.excel_tags = set()
        self.revit_tags = set()

    def errors(self):
        return [row for row in self.rows if row['status'] != config.PASS]


def _same(a, b):
    return a is not None and b is not None and abs(a - b) <= config.TOLERANCE_MM


def _size_text(width, height):
    def one(value):
        if value is None:
            return '?'
        return '{0:g}'.format(value)
    return '{0}x{1}'.format(one(width), one(height))


def _blank_row():
    return {'status': '', 'tag': '', 'sheet_number': NA, 'sheet_name': NA, 'view_name': NA,
            'element_id': None, 'annotation_id': None, 'sheet_id': None, 'view_id': None,
            'excel_width': None, 'excel_height': None, 'revit_width': None, 'revit_height': None,
            'quantity': None, 'description': '', 'comments': '', 'kind': '', 'text': '',
            'family': '', 'duplicate': False}


def _duplicate_key(scope, row):
    tag = row['tag']
    if row.get('kind') == 'Cable Tray':
        # segments of one run share a tag by design and are already grouped
        # into one row per size: never a duplicate
        return ('tray', tag, row['annotation_id'])
    if scope == 'project':
        return (tag,)
    if scope == 'sheet':
        return (tag, row['sheet_id'] if row['sheet_id'] is not None else ('view', row['view_id']))
    if scope == 'element':
        # an annotation that tags nothing (text note) can only be compared by view
        return (tag, row['view_id'], row['element_id'] if row['element_id'] is not None
                else ('annotation', row['annotation_id']))
    return (tag, row['view_id'])


def _merge_split_annotations(rows):
    """Join a NAME tag and a SIZE tag that sit on the same cable tray.

    Some projects annotate a tray with two tags: one tag type shows the
    calculation tag, another ("... SIZE TAG") shows 1x225x100 and the
    description. Read separately, the first has no size and the second has no
    calculation tag, so neither could be checked. When both are found on the
    same tray in the same view they are one annotation: the size, quantity and
    description of the size tag are moved onto the name tag's row and the size
    tag's own row is dropped.

    Only tags that point at a cable tray element can be paired this way.
    """
    by_host = {}
    for row in rows:
        if row['element_id'] is not None:
            by_host.setdefault((row['view_id'], row['element_id']), []).append(row)
    dropped = set()
    for members in by_host.values():
        if len(members) < 2:
            continue
        names = [row for row in members if row['_text']['tag'] and row['_text']['width'] is None]
        sizes = [row for row in members if not row['_text']['tag'] and row['_text']['width'] is not None]
        if not names or not sizes:
            continue
        for position, name_row in enumerate(names):
            size_row = sizes[min(position, len(sizes) - 1)]
            size = size_row['_text']
            name_row['revit_width'], name_row['revit_height'] = size['width'], size['height']
            name_row['quantity'] = size['quantity']
            name_row['description'] = size['description']
            name_row['_problems'] = []
            name_row['_comments'] = [comment for comment in name_row['_comments']
                                     if not comment.startswith('Size read from the cable tray element')]
            name_row['_comments'].append(
                u'Size read from a separate size tag on the same tray ({0}, id {1})'.format(
                    size_row.get('family') or 'size tag', size_row.get('annotation_id')))
        for size_row in sizes:
            dropped.add(id(size_row))
    return [row for row in rows if id(row) not in dropped]


def compare(excel, annotations, duplicate_scope=None, sheets_checked=None, sheets_total=None,
            readers=None, coverage=None):
    """Compare the calculation list with the annotations found in Revit.

    sheets_checked / sheets_total: set when only some sheets were scanned.
    MISSING then means "not on the checked sheets", and the row says so -
    the tag may well be annotated on a sheet that was left out.

    coverage: check that every tagged cable tray run is also TAGGED ON A SHEET.
        None    not checked
        'also'  annotations are results in their own right AND are used for this
        'only'  annotations are used for this alone and are not listed
                (the "Cable trays in the model" check)
    A run counts as tagged when a tag on a sheet points at one of its segments,
    or when an annotation shows its calculation tag."""
    readers = list(readers or [])
    sheets_partial = (sheets_checked is not None and sheets_total is not None
                      and sheets_checked < sheets_total)
    trays_too = 'cable_trays' in readers
    trays_only = readers == ['cable_trays']
    partial = (not trays_only and sheets_checked is not None and sheets_total is not None
               and sheets_checked < sheets_total)
    scope = duplicate_scope if duplicate_scope in config.DUPLICATE_SCOPES else config.DUPLICATE_SCOPE
    result = QAResult()
    excel_tags = excel.tags()
    result.excel_tags = excel_tags

    # ---- 1. parse every annotation --------------------------------------------
    rows = []
    for item in annotations:
        # a reader that already knows tag and size (the cable tray reader:
        # tag from a parameter, size from the element) hands them over ready
        parsed = item.get('parsed') or parse_annotation(item.get('text'), excel_tags)
        row = _blank_row()
        comments = []
        if item.get('kind') == 'Cable Tray':
            segments = item.get('segments') or 1
            comments.append('{0} cable tray segment(s) with this tag and size'.format(segments)
                            if parsed['tag'] else
                            '{0} cable tray segment(s) of this size have no calculation tag'.format(segments))
            row['element_ids'] = list(item.get('element_ids') or [])
        tag = parsed['tag']
        if tag is None and item.get('model_tag'):
            # the tag is not in the visible text: take it from the tagged tray
            tag = normalize_tag(item.get('model_tag'))
            if tag:
                comments.append('Tag read from the cable tray parameter, not from the annotation text')
        width, height = parsed['width'], parsed['height']
        model_width, model_height = item.get('model_width'), item.get('model_height')
        if width is None and model_width is not None:
            # no size in the text: fall back to the real tray
            width, height = model_width, model_height
            comments.append('Size read from the cable tray element, not from the annotation text')
        elif (config.COMPARE_MODEL_SIZE and width is not None and model_width is not None
              and not (_same(width, model_width) and _same(height, model_height))):
            comments.append('Annotation text shows {0} but the tagged cable tray is {1}'.format(
                _size_text(width, height), _size_text(model_width, model_height)))

        row.update({
            'tag': tag or '', 'revit_width': width, 'revit_height': height,
            'quantity': parsed['quantity'], 'description': parsed['description'],
            'kind': item.get('kind', ''), 'text': item.get('text') or '',
            'family': item.get('family') or item.get('kind', ''),
            'annotation_id': item.get('annotation_id'), 'element_id': item.get('element_id'),
            'view_id': item.get('view_id'), 'view_name': item.get('view_name') or NA,
            'sheet_id': item.get('sheet_id'), 'sheet_number': item.get('sheet_number') or NA,
            'sheet_name': item.get('sheet_name') or NA,
        })
        if item.get('other_sheets'):
            comments.append('View is also on sheet(s): ' + ', '.join(item['other_sheets']))
        if parsed['quantity'] and parsed['quantity'] > 1:
            comments.append('Quantity {0}: width and height are compared per tray'.format(
                parsed['quantity']))
        row['_problems'] = list(parsed['problems']) if not tag or width is None else []
        if not tag and 'no calculation tag found' not in row['_problems']:
            row['_problems'].append('no calculation tag found')
        row['_comments'] = comments
        row['_text'] = parsed                 # what the visible text alone gave
        rows.append(row)

    # ---- 1b. which trays carry a tag on a sheet -----------------------------------
    tagged_elements, covered_tags = set(), set()
    if coverage:
        for row in rows:
            if row['kind'] == 'Cable Tray':
                continue
            if row['element_id'] is not None:
                tagged_elements.add(row['element_id'])
            if row['tag']:
                covered_tags.add(row['tag'])
        for row in rows:
            if (row['kind'] == 'Cable Tray' and row['tag']
                    and tagged_elements.intersection(row.get('element_ids') or [])):
                covered_tags.add(row['tag'])        # one tagged segment covers the whole run
        if coverage == 'only':
            rows = [row for row in rows if row['kind'] == 'Cable Tray']
    annotations_listed = len([row for row in rows if row['kind'] != 'Cable Tray'])

    rows = _merge_split_annotations(rows)
    for row in rows:
        row.pop('_text', None)

    # ---- 2. duplicates (only annotations that have a tag) -------------------------
    groups = {}
    for row in rows:
        if row['tag']:
            groups.setdefault(_duplicate_key(scope, row), []).append(row)
    for members in groups.values():
        if len(members) > 1:
            for row in members:
                row['duplicate'] = True
                row['_comments'].append('Duplicate: this tag is annotated {0} times ({1})'.format(
                    len(members), config.DUPLICATE_SCOPE_LABELS[scope].lower()))

    # ---- 3. status per annotation -----------------------------------------------
    revit_tags = set()
    width_bad, height_bad, duplicate_tags, extra_tags = set(), set(), set(), set()
    invalid = 0
    invalid_by_family = {}
    not_tagged, not_tagged_only = set(), set()
    where_tagged = ('on the {0} checked sheet(s) ({1} other sheet(s) were not checked)'.format(
        sheets_checked, sheets_total - sheets_checked) if sheets_partial else 'on any sheet')
    for row in rows:
        tag = row['tag']
        comments = row.pop('_comments')
        problems = row.pop('_problems')
        if not tag or row['revit_width'] is None or row['revit_height'] is None:
            row['status'] = config.INVALID
            # show what the annotation actually says, so the cause is visible
            shown = u' / '.join(part.strip() for part in
                                (row['text'] or u'').replace('\r', '\n').split('\n') if part.strip())
            if len(shown) > 90:
                shown = shown[:87] + u'...'
            comments = [u'Could not read the annotation: {0}. Its text is: "{1}"'.format(
                '; '.join(problems or ['size missing']), shown)] + comments
            invalid += 1
            invalid_by_family[row['family']] = invalid_by_family.get(row['family'], 0) + 1
            if tag:
                revit_tags.add(tag)
                source = excel.by_tag.get(tag)
                if source is not None:
                    row['excel_width'], row['excel_height'] = source['width'], source['height']
        else:
            revit_tags.add(tag)
            source = excel.by_tag.get(tag)
            if source is None:
                row['status'] = config.EXTRA
                comments.insert(0, 'On cable trays in the model but not in the Excel calculation list'
                                if row.get('kind') == 'Cable Tray' else
                                'Annotated in Revit but not in the Excel calculation list')
                extra_tags.add(tag)
            else:
                row['excel_width'], row['excel_height'] = source['width'], source['height']
                comments = list(source['problems']) + comments
                width_ok = _same(source['width'], row['revit_width'])
                height_ok = _same(source['height'], row['revit_height'])
                if not width_ok:
                    width_bad.add(tag)
                if not height_ok:
                    height_bad.add(tag)
                if not width_ok and not height_ok:
                    row['status'] = config.BOTH_MISMATCH
                elif not width_ok:
                    row['status'] = config.WIDTH_MISMATCH
                elif not height_ok:
                    row['status'] = config.HEIGHT_MISMATCH
                elif row['duplicate']:
                    row['status'] = config.DUPLICATE
                else:
                    row['status'] = config.PASS
                if not width_ok or not height_ok:
                    comments.insert(0, 'Excel {0}, Revit {1}'.format(
                        _size_text(source['width'], source['height']),
                        _size_text(row['revit_width'], row['revit_height'])))
                if tag in excel.duplicates:
                    comments.append('Tag is listed {0} times in the Excel (rows {1}); the first '
                                    'row is used'.format(len(excel.duplicates[tag]),
                                                         ', '.join(str(n) for n in excel.duplicates[tag])))
        if row['duplicate'] and tag:
            duplicate_tags.add(tag)
        if coverage and tag and row.get('kind') == 'Cable Tray':
            segments = row.get('element_ids') or []
            if tag in covered_tags:
                with_tag = len(tagged_elements.intersection(segments))
                comments.append('{0} of {1} segment(s) carry a tag {2}'.format(
                    with_tag, len(segments), where_tagged) if with_tag else
                    'Tagged on a sheet through another segment or size of this run')
            else:
                row['not_tagged'] = True
                not_tagged.add(tag)
                if row['status'] == config.PASS:
                    # the tray itself is right: the missing tag is its only issue
                    row['status'] = config.NOT_TAGGED
                    not_tagged_only.add(tag)
                    comments.insert(0, 'The cable tray has this calculation tag in its parameter '
                                       'but is not tagged {0}'.format(where_tagged))
                else:
                    comments.append('Also not tagged {0}'.format(where_tagged))
        row['comments'] = '. '.join(comments)

    # ---- 4. Excel tags with no annotation ------------------------------------------
    missing = sorted(excel_tags - revit_tags)
    for tag in missing:
        source = excel.by_tag[tag]
        row = _blank_row()
        if partial:
            where = ('not annotated on the {0} checked sheet(s) - {1} other sheet(s) were not '
                     'checked'.format(sheets_checked, sheets_total - sheets_checked))
        else:
            where = 'not annotated on any sheet'
        if trays_only:
            where = 'not found in the tag parameter of any cable tray in the model'
        elif trays_too:
            where += ' and not found on any cable tray'
        comments = ['In the Excel calculation list (row {0}) but {1}'.format(
            source['row'], where)] + list(source['problems'])
        row.update({'status': config.MISSING, 'tag': tag, 'excel_width': source['width'],
                    'excel_height': source['height'], 'comments': '. '.join(comments)})
        rows.append(row)

    order = dict((status, index) for index, status in enumerate(config.STATUS_ORDER))
    rows.sort(key=lambda r: (order.get(r['status'], 99), r['tag'], r['sheet_number'], r['view_name']))
    for index, row in enumerate(rows):
        row['key'] = index

    matched = excel_tags & revit_tags
    result.rows = rows
    result.revit_tags = revit_tags
    result.summary = {
        'excel_tags': len(excel_tags),
        'revit_tags': len(revit_tags),
        'annotations': len(annotations) if coverage != 'only' else len(rows) - len(missing),
        'annotations_listed': annotations_listed,
        'not_tagged': len(not_tagged),
        'coverage': coverage,
        'matched': len(matched),
        'missing': len(missing),
        'extra': len(extra_tags),
        'width_mismatch': len(width_bad),
        'height_mismatch': len(height_bad),
        'duplicates': len(duplicate_tags),
        'invalid': invalid,
        'passed': len([r for r in rows if r['status'] == config.PASS]),
        'excel_duplicates': len(excel.duplicates),
        'duplicate_scope': scope,
        'invalid_by_family': invalid_by_family,
        'sheets_checked': sheets_checked,
        'sheets_total': sheets_total,
        'partial': partial,
    }
    summary = result.summary
    summary['errors'] = (summary['missing'] + summary['extra'] + len(width_bad | height_bad)
                         + summary['duplicates'] + summary['invalid'] + len(not_tagged_only))
    return result


def summary_text(summary):
    """The text block shown in the window and written to the report."""
    lines = [
        'CABLE TRAY QA/QC',
        '=============================',
        '',
        'Excel Calculation Tags : {0}'.format(summary['excel_tags']),
        'Revit Annotation Tags  : {0}'.format(summary['revit_tags']),
        '',
        'Matched                : {0}'.format(summary['matched']),
        'Missing                : {0}'.format(summary['missing']),
        'Extra                  : {0}'.format(summary['extra']),
        'Width Mismatch         : {0}'.format(summary['width_mismatch']),
        'Height Mismatch        : {0}'.format(summary['height_mismatch']),
        'Duplicates             : {0}'.format(summary['duplicates']),
        'Invalid Annotations    : {0}'.format(summary['invalid']),
        'Not Tagged On Sheet    : {0}'.format(summary.get('not_tagged', 0)),
        'Overall Errors         : {0}'.format(summary['errors']),
    ]
    return '\n'.join(lines)
