# -*- coding: utf-8 -*-
"""Cable Tray QA/QC - annotation text parser (pure Python, no Revit API).

Turns the text of one annotation, e.g.

    CT-MCC-CC-N-B1-03-01L-04
    1x225x100 LV SUBMAIN TRAY LIFE SAFETY

into  TAG / QUANTITY / WIDTH / HEIGHT / DESCRIPTION.

The parser never looks for particular words. It finds
  * the SIZE by its shape (number x number x number), and
  * the TAG either because the Excel knows it, or because it is the first
    token shaped like a tag (letters and digits joined by hyphens).
Everything after the size on the same line is the description, whatever it says.
"""
import re

import config

_LINE_BREAK = re.compile(r'\r\n|\r|\n|\v|\f| | ')
_SPACES = re.compile(r'\s+')
# every dash-like character people paste into a tag
_DASHES = re.compile(u'[‐‑‒–—―−﹘﹣－]')
_SIZE_QWH = re.compile(config.SIZE_REGEX_QWH, re.UNICODE)
_SIZE_WH = re.compile(config.SIZE_REGEX_WH, re.UNICODE)
_TAG_SHAPE = re.compile(r'^[A-Z0-9]+(?:[-_/.][A-Z0-9]+)+$')
_TRIM = u' \t-:;,.|/'


def normalize_tag(value):
    """The ONE normalisation used for Excel tags and Revit tags alike:
    text -> trimmed, UPPER CASE, no spaces around hyphens, no double spaces.
    Hyphens stay part of the tag; look-alike dashes become a plain hyphen so a
    tag pasted from Word still matches."""
    if value is None:
        return u''
    try:
        text = u'{0}'.format(value)
    except Exception:
        text = str(value)
    text = _DASHES.sub(u'-', text)
    text = text.replace(u' ', u' ')
    text = _SPACES.sub(u' ', text).strip().upper()
    text = re.sub(r'\s*-\s*', u'-', text)
    return text


def to_number(value):
    """'900', '900 mm', 900.0, '1,200' -> float; anything else -> None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = u'{0}'.format(value).strip().replace(u',', u'')
    match = re.search(r'-?\d+(?:\.\d+)?', text)
    if not match:
        return None
    try:
        return float(match.group())
    except ValueError:
        return None


def _looks_like_tag(token):
    """Letters AND digits joined by at least one hyphen: CT-CC-B01-N-01-03L."""
    if not _TAG_SHAPE.match(token):
        return False
    return bool(re.search(r'[A-Z]', token)) and bool(re.search(r'\d', token))


def _find_size(lines):
    """(line index, match, quantity, width, height) of the first size found."""
    for pattern, with_quantity in ((_SIZE_QWH, True), (_SIZE_WH, False)):
        for index, line in enumerate(lines):
            match = pattern.search(line)
            if not match:
                continue
            if with_quantity:
                return (index, match, int(match.group(1)),
                        float(match.group(2)), float(match.group(3)))
            return index, match, 1, float(match.group(1)), float(match.group(2))
    return None


def _find_tag(lines, size, known_tags):
    """The calculation tag, by the three rules described in the module text."""
    # the text that is NOT the size: whole lines, and the part before the size
    pieces = []
    for index, line in enumerate(lines):
        if size is not None and index == size[0]:
            before = line[:size[1].start()]
            if before.strip():
                pieces.append(before)
        else:
            pieces.append(line)

    candidates = []
    for piece in pieces:
        whole = normalize_tag(piece)
        if whole:
            candidates.append(whole)
        for token in piece.split():
            token = normalize_tag(token).strip(_TRIM.replace(u'-', u''))
            if token and token not in candidates:
                candidates.append(token)

    # 1. a tag the Excel knows
    if known_tags:
        for candidate in candidates:
            if candidate in known_tags:
                return candidate, 'excel'
    # 2. a project-specific pattern from config
    if config.TAG_REGEX:
        for piece in pieces:
            match = re.search(config.TAG_REGEX, normalize_tag(piece))
            if match:
                return normalize_tag(match.group(1) if match.groups() else match.group()), 'pattern'
    # 3. the first token shaped like a tag
    for candidate in candidates:
        if _looks_like_tag(candidate):
            return candidate, 'shape'
    return None, None


def parse_annotation(text, known_tags=None):
    """Parse one annotation.

    known_tags: set of normalised Excel tags (optional but recommended - it is
                what makes tag detection independent of any naming format).

    Returns a dict:
        tag          normalised calculation tag, or None
        tag_source   'excel' / 'pattern' / 'shape' / None
        quantity     int or None
        width        float (mm) or None
        height       float (mm) or None
        description  text after the size (may be '')
        problems     list of human readable problems (empty = clean)
    """
    result = {'tag': None, 'tag_source': None, 'quantity': None, 'width': None,
              'height': None, 'description': u'', 'problems': []}
    raw = u'' if text is None else u'{0}'.format(text)
    lines = [_SPACES.sub(u' ', line).strip() for line in _LINE_BREAK.split(raw)]
    lines = [line for line in lines if line]
    if not lines:
        result['problems'].append('annotation is empty')
        return result

    size = _find_size(lines)
    if size is not None:
        index, match, quantity, width, height = size
        result['quantity'], result['width'], result['height'] = quantity, width, height
        after = lines[index][match.end():].strip(_TRIM)
        extra = [line for position, line in enumerate(lines) if position > index]
        result['description'] = u' '.join([part for part in [after] + extra if part])
    else:
        result['problems'].append('no size (quantity x width x height) found')

    tag, source = _find_tag(lines, size, known_tags)
    result['tag'], result['tag_source'] = tag, source
    if tag is None:
        result['problems'].append('no calculation tag found')
    return result


def could_be_tray_annotation(text, known_tags=None):
    """Cheap pre-filter for Text Notes: does this text deserve parsing at all?"""
    parsed = parse_annotation(text, known_tags)
    if parsed['width'] is not None and parsed['tag'] is not None:
        return True
    return parsed['tag_source'] == 'excel'
