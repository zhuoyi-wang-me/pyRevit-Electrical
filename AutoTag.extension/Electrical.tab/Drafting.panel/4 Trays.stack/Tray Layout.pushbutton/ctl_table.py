# -*- coding: utf-8 -*-
"""Cable Tray Layout - the cable table of ALL trays, for a view of its own.

    CABLE TAG | FED FROM | FED TO | CABLE

One block per Tray ID: the Tray ID once, in a merged cell beside its cables;
an empty row between two blocks. No Revit in this file: compose_table() draws
on a "canvas" exactly as ctl_drawing does (line / text, millimetres, y up),
so the same RevitCanvas makes the detail lines and text notes.

This module is separate from the tray drawings on purpose: the table has its
own view, its own scale and its own text style, and nothing in the tray
drawing code depends on it.
"""

HEADINGS = (u'CABLE TAG', u'FED FROM', u'FED TO', u'CABLE')
CHAR_WIDTH = 0.9                          # letter width as a share of the text height, on the wide side
ROW_HEIGHT = 2.0                          # in text heights
PAD = 0.8                                 # space left of a text in its cell, in text heights


def table_rows(tray):
    """[(fed from, fed to, cable)] of one tray, as shown in the table."""
    lines = []
    for row in tray.rows:
        cable = row.cable_type or u'-'
        # a quantity from a Quantity column is shown; "2x4C..." already says it in the text
        if row.quantity > 1 and not getattr(row, 'quantity_in_text', False):
            cable = u'{0} x  {1}'.format(row.quantity, cable)
        lines.append((row.fed_from or u'-', row.fed_to or u'-', cable))
    return lines


def column_edges(trays, h):
    """The x of the five vertical lines: every column as wide as its longest text."""
    longest = [len(heading) for heading in HEADINGS]
    for tray in trays:
        longest[0] = max(longest[0], len(u'{0}'.format(tray.id)))
        for line in table_rows(tray):
            for index, text in enumerate(line):
                longest[index + 1] = max(longest[index + 1], len(text))
    edges = [0.0]
    for count in longest:
        edges.append(edges[-1] + h * (CHAR_WIDTH * count + 2.0 * PAD))
    return edges


def tag_of(tray):
    """What stands in the CABLE TAG cell: the Tray ID, with "(tray 1 of 2)"
    when the Tray ID stands for several trays."""
    return u'{0}'.format(getattr(tray, 'label', None) or tray.id)


def compose_tables(canvas, trays, h):
    """One SEPARATE table per tray, one below the other - the same small table
    that stands under each tray drawing: its own heading row, the tray in the
    merged first cell, its cables beside it. `trays` are the trays as drawn
    (a Tray ID split over two trays gives two tables, each with the cables
    that lie in that tray). All tables share the column widths, so they line
    up. Returns the y of the bottom."""
    trays = [tray for tray in trays if tray.rows]
    if not trays:
        return 0.0
    edges = column_edges(trays, h)
    # the first column must also hold the longer "(tray 1 of 2)" names
    widest_tag = max([len(HEADINGS[0])] + [len(tag_of(tray)) for tray in trays])
    shift = h * (CHAR_WIDTH * widest_tag + 2.0 * PAD) - edges[1]
    if shift > 0:
        edges = [edges[0]] + [x + shift for x in edges[1:]]
    left, tag_right, right = edges[0], edges[1], edges[-1]
    step = ROW_HEIGHT * h
    y = 0.0
    for number, tray in enumerate(trays):
        lines = table_rows(tray)
        top = y
        head_bottom = top - step
        bottom = head_bottom - step * len(lines)
        # frame, heading line and column lines
        for line_y in (top, head_bottom, bottom):
            canvas.line(left, line_y, right, line_y, 'thin')
        for x in edges:
            canvas.line(x, top, x, bottom, 'thin')
        for index, heading in enumerate(HEADINGS):
            canvas.text((edges[index] + edges[index + 1]) / 2.0, top - 0.35 * h, heading, 'center')
        for index, line in enumerate(lines):
            row_top = head_bottom - step * index
            for column, text in enumerate(line):
                canvas.text(edges[column + 1] + PAD * h, row_top - 0.35 * h, text, 'left')
            if index:                                      # between the rows - not through the merged first cell
                canvas.line(tag_right, row_top, right, row_top, 'thin')
        canvas.text((left + tag_right) / 2.0, (head_bottom + bottom) / 2.0 + 0.65 * h, tag_of(tray), 'center')
        y = bottom
        if number < len(trays) - 1:
            y -= 1.5 * step                                # free space before the next tray's table
    return y


def compose_table(canvas, trays, h):
    """ONE table for all trays: the headings once, then one block per Tray ID
    with an empty row between two blocks. h = text height in model millimetres
    (paper text size x view scale). The top left corner is at (0, 0); the
    table grows downwards. Returns the y of its bottom."""
    trays = [tray for tray in trays if tray.rows]
    if not trays:
        return 0.0
    edges = column_edges(trays, h)
    left, tag_right, right = edges[0], edges[1], edges[-1]
    step = ROW_HEIGHT * h

    # --- headings ---------------------------------------------------------
    y = 0.0
    canvas.line(left, y, right, y, 'thin')
    for index, heading in enumerate(HEADINGS):
        canvas.text((edges[index] + edges[index + 1]) / 2.0, y - 0.35 * h, heading, 'center')
    y -= step
    canvas.line(left, y, right, y, 'thin')
    for x in edges:
        canvas.line(x, 0.0, x, y, 'thin')

    # --- one block per tray -------------------------------------------------
    for number, tray in enumerate(trays):
        lines = table_rows(tray)
        top = y
        bottom = top - step * len(lines)
        for index, line in enumerate(lines):
            row_top = top - step * index
            for column, text in enumerate(line):
                canvas.text(edges[column + 1] + PAD * h, row_top - 0.35 * h, text, 'left')
            if index:                                      # between the rows - not through the merged first cell
                canvas.line(tag_right, row_top, right, row_top, 'thin')
        # the Tray ID in the middle of its merged cell
        canvas.text((left + tag_right) / 2.0, (top + bottom) / 2.0 + 0.65 * h, u'{0}'.format(tray.id), 'center')
        for x in edges:
            canvas.line(x, top, x, bottom, 'thin')
        canvas.line(left, bottom, right, bottom, 'thin')
        y = bottom
        if number < len(trays) - 1:
            # an empty row between two trays: only the outside lines and the first column go on
            y -= step
            for x in (left, tag_right, right):
                canvas.line(x, bottom, x, y, 'thin')
            canvas.line(left, y, right, y, 'thin')
    return y
