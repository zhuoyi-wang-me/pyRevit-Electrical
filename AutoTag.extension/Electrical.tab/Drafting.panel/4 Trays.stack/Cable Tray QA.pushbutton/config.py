# -*- coding: utf-8 -*-
"""Cable Tray QA/QC - configuration.

Everything a project is likely to need to change lives here. Nothing in this
file touches the Revit API, so it can be edited safely and imported by the
pure-Python modules (parser, comparison, Excel reader, report).
"""

# =============================================================================
# EXCEL
# =============================================================================
# Header names are matched case-insensitively, ignoring spaces, underscores,
# hyphens and dots, so "Calculation_Tag", "Calculation Tag" and
# "CALCULATION-TAG" are the same header.
TAG_HEADERS = ['Calculation_Tag', 'Calculation Tag', 'Calc Tag', 'Cable Tray Tag',
               'Tray Tag', 'Tag']
WIDTH_HEADERS = ['Width', 'Tray Width', 'Width (mm)', 'W']
HEIGHT_HEADERS = ['Height', 'Tray Height', 'Height (mm)', 'Depth', 'H']

# The header row does not have to be row 1: the first rows of every sheet are
# searched for a row that holds all three headers.
HEADER_SCAN_ROWS = 30

# None = use the first worksheet that has the three headers.
# Set a name (e.g. 'Tray Calc') to force one worksheet.
EXCEL_SHEET_NAME = None

# =============================================================================
# REVIT ANNOTATION READERS
# =============================================================================
# Reader 1 (preferred): real Revit tags (IndependentTag) of these categories.
TAG_CATEGORIES = ['OST_CableTrayTags', 'OST_CableTrayFittingTags', 'OST_MultiCategoryTags']
# A multi-category tag only counts when it tags an element of these categories.
TRAY_CATEGORIES = ['OST_CableTray', 'OST_CableTrayFitting']

# Reader 2 (optional): Text Notes. Only notes that look like a tray annotation
# (contain a size such as 1x225x100, or a tag found in the Excel) are used.
# Reader 3 (optional): Generic Annotation families whose text is held in
# parameters. The values of these parameters are read, in this order, one per
# line, and then parsed exactly like a tag's text. The FIRST parameter should
# hold the calculation tag, the SECOND the size / description line.
GENERIC_ANNOTATION_PARAMETERS = ['Calculation_Tag', 'Tray Size']
# Only families whose name contains this text are read ('' = every family).
GENERIC_ANNOTATION_FAMILY_CONTAINS = ''

# What is checked the first time the tool is used (the window then remembers
# the last choice):
#   'model'   the cable trays in the model  (tag from a tray parameter, real
#             width / height; sheets and views play no part)
#   'sheets'  the cable tray annotations on the sheets
#   'both'
CHECK_MODE = 'model'

# Which annotation readers are ticked when the window opens.
READ_TAGS = True
READ_TEXT_NOTES = False
READ_GENERIC_ANNOTATIONS = False

# Only annotations in views that are placed on a sheet (or drawn directly on a
# sheet) are checked. Set False to check every view in the project.
ONLY_VIEWS_ON_SHEETS = True

# When a tag's text does not contain a calculation tag, these parameters of
# the TAGGED CABLE TRAY are tried (instance first, then type).
TRAY_TAG_PARAMETERS = ['Calculation_Tag', 'Calculation Tag', 'Cable Tray Tag']

# =============================================================================
# PARSER
# =============================================================================
# None = automatic: a token that is a tag in the Excel wins; otherwise the
# first token that looks like a tag (letters + digits joined by hyphens).
# Set a regular expression with ONE group to force a project format, e.g.
#   TAG_REGEX = r'\b(CT-[A-Z0-9-]+)\b'
TAG_REGEX = None

# Quantity x Width x Height, e.g. "1x225x100", "2 X 300 X 100", "1×150×100".
SIZE_REGEX_QWH = r'(?<![\w.])(\d{1,3})\s*[xX×*]\s*(\d{2,4}(?:\.\d+)?)\s*[xX×*]\s*(\d{2,4}(?:\.\d+)?)(?![\w.])'
# Width x Height only, e.g. "225x100" (quantity is then 1).
SIZE_REGEX_WH = r'(?<![\w.])(\d{2,4}(?:\.\d+)?)\s*[xX×*]\s*(\d{2,4}(?:\.\d+)?)(?![\w.])'

# =============================================================================
# COMPARISON
# =============================================================================
# Sizes within this many millimetres are the same size.
TOLERANCE_MM = 0.5

# What counts as a DUPLICATE annotation (the same calculation tag more than once):
#   'element'  on the same cable tray in the same view   (certainly a mistake)
#   'view'     anywhere in the same view
#   'sheet'    anywhere on the same sheet
#   'project'  anywhere in the project
DUPLICATE_SCOPES = ['element', 'view', 'sheet', 'project']
DUPLICATE_SCOPE_LABELS = {
    'element': 'Same tray tagged twice in one view',
    'view': 'Same tag twice in one view',
    'sheet': 'Same tag twice on one sheet',
    'project': 'Same tag twice anywhere in the project',
}
DUPLICATE_SCOPE = 'view'

# Also compare the size written in the annotation with the real size of the
# tagged cable tray and mention a difference in the Comments column.
COMPARE_MODEL_SIZE = True

# =============================================================================
# STATUSES AND COLOURS (report + window)
# =============================================================================
PASS = 'PASS'
MISSING = 'MISSING'
EXTRA = 'EXTRA'
WIDTH_MISMATCH = 'WIDTH MISMATCH'
HEIGHT_MISMATCH = 'HEIGHT MISMATCH'
BOTH_MISMATCH = 'WIDTH + HEIGHT MISMATCH'
DUPLICATE = 'DUPLICATE'
INVALID = 'INVALID ANNOTATION'
# A cable tray whose parameter holds a calculation tag (and whose size is
# right), but which carries no tag on any checked sheet.
NOT_TAGGED = 'NOT TAGGED ON SHEET'

STATUS_ORDER = [MISSING, BOTH_MISMATCH, WIDTH_MISMATCH, HEIGHT_MISMATCH,
                EXTRA, NOT_TAGGED, DUPLICATE, INVALID, PASS]

# Flag cable trays that are not tagged on a sheet (the window remembers the
# last choice). In "Cable trays in the model" mode the tags on the sheets are
# then read as well, but only to see which trays carry one.
FLAG_NOT_TAGGED = True

# status -> (fill colour, text colour) as RRGGBB
STATUS_COLOURS = {
    PASS: ('C6EFCE', '006100'),
    MISSING: ('FFC7CE', '9C0006'),
    EXTRA: ('FFD9A8', '8A4B00'),
    WIDTH_MISMATCH: ('FFC7CE', '9C0006'),
    HEIGHT_MISMATCH: ('FFC7CE', '9C0006'),
    BOTH_MISMATCH: ('FF9AA2', '7A0006'),
    DUPLICATE: ('FFEB9C', '7F6000'),
    INVALID: ('E1D5F0', '4B2E83'),
    NOT_TAGGED: ('FBD0F0', '8A0A6B'),
}

# "Colour by issue": the colour each kind of issue gets IN THE MODEL VIEW
# (red, green, blue). Strong colours, one per issue, so they can be told apart
# on a drawing. MISSING has no colour: a missing tag has no element.
MODEL_COLOURS = {
    BOTH_MISMATCH: (150, 0, 200),       # purple
    WIDTH_MISMATCH: (255, 0, 0),        # red
    HEIGHT_MISMATCH: (0, 110, 255),     # blue
    EXTRA: (255, 140, 0),               # orange
    NOT_TAGGED: (255, 0, 190),          # magenta
    DUPLICATE: (200, 165, 0),           # dark yellow
    INVALID: (0, 175, 190),             # cyan
    PASS: (0, 170, 70),                 # green (only when PASS itself is shown)
}
# line weight of a coloured element (1-16); 0 = leave the weight alone
MODEL_COLOUR_LINE_WEIGHT = 6

NOT_AVAILABLE = '---'
TITLE = 'Cable Tray QA/QC'
