# -*- coding: utf-8 -*-
"""Earthing Route Generator - settings a project may want to change.

Nothing here touches the Revit API.
"""
TITLE = 'Earthing Route Generator'

# ---------------------------------------------------------------- defaults (mm)
CLEARANCE_MM = 100.0
GRID_MM = 100.0
GRID_CHOICES_MM = [50, 100, 150, 200]        # the list is editable: type any value

# How far the search region reaches beyond a panel and the earth bar. The
# next one is tried when no route is found; the grid never covers more.
BUFFERS_MM = [2000.0, 6000.0, 15000.0]

# A panel on a wall sits inside the wall's clearance. Within this distance of
# the panel and of the earth bar the route may leave / enter such a zone.
ESCAPE_MM = 600.0

# One 90 degree turn costs as much as this length of line: higher = fewer bends.
TURN_PENALTY_MM = 600.0

# When several lines enter one earth bar the grid is refined to the distance
# between them on the bar, but never below this.
MIN_AUTO_GRID_MM = 20.0

# The grid is coarsened (and the status bar says so) rather than exceed this.
MAX_GRID_CELLS = 800000

# ---------------------------------------------------------------- obstacles
# (label in the window, group, Revit BuiltInCategory names, ticked by default)
OBSTACLES = [
    ('Columns', 'Structural', ['OST_StructuralColumns', 'OST_Columns'], True),
    ('Structural Framing', 'Structural', ['OST_StructuralFraming'], True),
    ('Walls', 'Structural', ['OST_Walls'], True),
    ('Ducts', 'Mechanical', ['OST_DuctCurves', 'OST_FlexDuctCurves'], True),
    ('Duct Fittings', 'Mechanical', ['OST_DuctFitting', 'OST_DuctAccessory'], True),
    ('Pipes', 'Mechanical', ['OST_PipeCurves', 'OST_FlexPipeCurves'], True),
    ('Pipe Fittings', 'Mechanical', ['OST_PipeFitting', 'OST_PipeAccessory'], True),
    ('Mechanical Equipment', 'Mechanical', ['OST_MechanicalEquipment'], True),
    ('Air Terminals', 'Mechanical', ['OST_DuctTerminal'], True),
    ('Cable Trays', 'Electrical', ['OST_CableTray'], True),
    ('Cable Tray Fittings', 'Electrical', ['OST_CableTrayFitting'], True),
    ('Conduits', 'Electrical', ['OST_Conduit'], True),
    ('Conduit Fittings', 'Electrical', ['OST_ConduitFitting'], True),
    ('Electrical Equipment', 'Electrical', ['OST_ElectricalEquipment'], True),
]
WALL_CATEGORIES = ['OST_Walls']
DOOR_CATEGORIES = ['OST_Doors']
INCLUDE_LINKS = True
# How linked elements are found.
#   False (default)  the link's own elements between the heights of the plan's
#                    view range - the long-established, stable way
#   True             "what this view shows of that link" (a Revit 2024+ query).
#                    It follows the view's visibility settings more closely,
#                    but Revit 2025.4 closed without a message while reading
#                    obstacles through it on a cloud model, so it is OFF.
LINK_VIEW_QUERY = False
# With Walls on, door openings are left open so a route can pass from room to
# room. Off = a wall is solid along its whole length.
ROUTE_THROUGH_DOORS = True

# True: a line starts on the EDGE of the panel and ends on the edge of the
# earth bar (their outline in the plan). False: centre / connector point.
CONNECT_AT_EDGE = True

# True: a line that meets the earthing line of another panel (to the same
# earth bar) ends there in a tee, so a shared stretch is ONE line.
# False: every panel gets its own full line to the earth bar; lines that take
# the same way then lie on top of each other.
JOIN_SHARED_RUNS = True
# What happens where the lines of several panels take the same way:
#   'offset'   separate lines, side by side at PARALLEL_OFFSET_MM
#   'join'     one line with tees
#   'overlap'  separate lines, on top of each other
SHARED_RUNS = 'offset'
PARALLEL_OFFSET_MM = 100.0

# True: the lines enter the earth bar square to its long side, each on its
# own spot, from the side the panels are on. False: straight to the bar from
# wherever the route arrives.
BAR_ENTRY_SQUARE = True

# Where a line has to cross other earthing lines it is interrupted and gets a
# break mark on both sides. Sizes are in millimetres ON PAPER (they are
# multiplied by the view scale), so the mark looks the same at 1:50 and 1:100.
BREAK_AT_CROSSINGS = True
BREAK_MARGIN_PAPER_MM = 1.5        # distance from the crossed lines to where the line stops
BREAK_HALF_WIDTH_PAPER_MM = 1.2    # half the width of the zigzag
BREAK_HEIGHT_PAPER_MM = 1.0        # its height along the line
# Every square corner of a line is drawn as a bend (a detail arc) of this
# radius, in model millimetres. 0 = sharp corners. A bend never takes more than
# half of a short leg, so corners close together get a smaller bend.
BEND_RADIUS_MM = 150.0
# A loop of earth bars goes back from the last bar to the first.
CLOSE_LOOP = True

# ---------------------------------------------------------------- lines
# The first line style whose name contains one of these is preselected.
LINE_STYLE_HINTS = ['earth', 'grounding', 'ground']
# Line styles that cannot be given to a detail line.
LINE_STYLES_EXCLUDED = ['<Room Separation>', '<Area Boundary>', '<Space Separation>', '<Sketch>',
                        '<Insulation Batting Lines>', '<Fabric Envelope>', '<Fabric Sheets>',
                        '<Path of Travel Lines>', '<Lines>', '<Axis of Rotation>', '<Load Area Separation>']
# How a preview line looks (red, green, blue) + line weight 1-16.
PREVIEW_COLOUR = (255, 0, 200)
PREVIEW_WEIGHT = 6

FEET_PER_MM = 1.0 / 304.8

# how far round an earth bar / panel / exit point the room's walls decide which way the lines run (mm)
ROOM_ANGLE_RADIUS_MM = 8000.0
