# Cable Tray QA/QC – pyRevit extension

Checks the cable tray calculation list (Excel) against the cable tray annotations on the Revit sheets.

**Read-only.** The tool never opens a transaction. "Highlight" uses Revit's ordinary selection.

Target: Revit 2025 / 2026, pyRevit 5.2+, IronPython 2.7 engine (the pyRevit default).

---

## 1. Folder structure

```
CableTrayQA.extension/
├── extension.json                     extension metadata (pyRevit reads .json, not .yaml)
├── README.md
└── Electrical.tab/
    └── QA QC.panel/
        ├── bundle.yaml                panel layout
        └── Cable Tray QA.pushbutton/
            ├── bundle.yaml            button title / tooltip
            ├── icon.png, icon.dark.png
            ├── script.py              window + orchestration
            ├── ui.xaml                the window
            ├── config.py              everything a project may need to change
            ├── excel_reader.py        reads the calculation workbook
            ├── revit_reader.py        reads the annotations from Revit
            ├── annotation_parser.py   text -> tag / quantity / width / height / description
            ├── comparison.py          Excel versus Revit, statuses, summary
            ├── highlight.py           select / open sheet / open view (ExternalEvent)
            └── report.py              writes the Excel QA report
```

pyRevit only loads a button that sits inside a `.tab` and a `.panel` folder, which is why the pushbutton is not directly under the extension folder.

## 2. Installation

1. Copy `CableTrayQA.extension` to your extensions folder (the folder that already holds your other `.extension` folders).
2. If that folder is not registered yet: pyRevit tab → Settings → Custom Extension Directories → add the **parent** folder → Save Settings and Reload.
3. Otherwise just reload pyRevit. The button is at **Electrical tab → QA QC → Cable Tray QA/QC**.

## 3. What each file does

| File | Role | Touches Revit? |
|---|---|---|
| `script.py` | The window, the buttons, and the order of the steps. No checking logic. | through the other modules |
| `config.py` | Header names, tag categories, parser patterns, tolerance, duplicate rule, statuses and colours. | no |
| `excel_reader.py` | Opens .xlsx / .xlsm / .csv, finds the header row, normalises tags, converts sizes to numbers. | no |
| `revit_reader.py` | Indexes sheets and views once, then reads tags / text notes / generic annotations. | read-only |
| `annotation_parser.py` | Turns annotation text into TAG, QUANTITY, WIDTH, HEIGHT, DESCRIPTION. | no |
| `comparison.py` | Set comparison, size check, duplicates, status per row, summary. | no |
| `highlight.py` | Selection, open sheet, open view; the ExternalEvent wrapper for the modeless window. | selection / view switch only |
| `report.py` | Writes the coloured .xlsx report. | no |

## 4. Excel requirements

- File type: `.xlsx`, `.xlsm` or `.csv`. Old `.xls` must be saved as `.xlsx`. Excel does not need to be installed.
- One header row with three columns: **Calculation_Tag**, **Width**, **Height**.
- The header row can be anywhere in the first 30 rows of any visible worksheet; the first worksheet that has all three headers is used. Other columns are ignored.
- Header matching ignores case, spaces, underscores and hyphens. Accepted alternatives (`config.py`): *Calculation Tag, Calc Tag, Cable Tray Tag, Tray Tag, Tag* / *Tray Width, Width (mm), W* / *Tray Height, Height (mm), Depth, H*.
- Width and Height are millimetres. `900`, `900.0` and `900 mm` are all read as 900. Formula cells use their calculated value.
- Rows without a tag are skipped (and counted). A tag listed twice is reported; the first row is used.
- If a column is missing the tool stops with a message that names it.

## 5. Revit annotation assumptions

These are the assumptions the tool makes. **Check the first two against your project before trusting the results.**

1. **The annotation is a real Revit tag** (`IndependentTag`) of category *Cable Tray Tags*, *Cable Tray Fitting Tags*, or a *Multi-Category Tag* placed on a cable tray. This is the default and preferred reader.
2. **The tag's visible text contains the calculation tag and the size**, as `Quantity x Width x Height` (for example `1x225x100`). The text is read from `IndependentTag.TagText`, which is exactly what the tag family's label shows. The two parts may be on two lines or on one.
3. The tagged cable tray is read as well: its element id, and its real width and height (`RBS_CABLETRAY_WIDTH_PARAM`, `RBS_CABLETRAY_HEIGHT_PARAM`).
4. If the tag text has **no calculation tag**, the tool looks for a parameter on the tray named `Calculation_Tag`, `Calculation Tag` or `Cable Tray Tag` (`config.TRAY_TAG_PARAMETERS`).
5. If the tag text has **no size**, the tray's real size is used and the Comments column says so.
6. Only annotations in views placed on a sheet (or drawn on the sheet itself) are checked. Untick *Only views placed on sheets* to check every view.
7. Tags of trays in **linked models** are read by their text only (there is no local tray element).

Alternative readers (tick them in the window; switch the defaults in `config.py`):

- **Text notes.** A note is used only if it contains a size and a tag-shaped token, or a tag that is in the Excel. Other notes are ignored.
- **Generic annotation families.** The text must be in parameters. Set their names in `config.GENERIC_ANNOTATION_PARAMETERS` – **first the parameter that holds the calculation tag, then the one that holds the size / description line** (defaults: `Calculation_Tag`, `Tray Size`). `GENERIC_ANNOTATION_FAMILY_CONTAINS` limits the reader to families whose name contains a given text.

To support another kind of annotation, add a function to `revit_reader.py` that returns records in the same shape and list it in `READERS`.

## 6. How the parser works

Input: the annotation text. Output: TAG, QUANTITY, WIDTH, HEIGHT, DESCRIPTION.

1. The text is split into lines; spaces are collapsed.
2. **Size**: the first `number x number x number` (also `X`, `×`, `*`, with or without spaces) gives quantity, width, height. If there is none, `number x number` gives width and height with quantity 1.
3. **Description**: whatever follows the size. No words are looked for, so LIFE SAFETY, ESSENTIAL, SUBMAIN and anything else are all just description.
4. **Tag**, in this order:
   1. a line or word that **is a tag in the Excel** (this is what makes the parser independent of any naming format);
   2. the pattern in `config.TAG_REGEX`, if you set one;
   3. the first word shaped like a tag: letters and digits joined by hyphens.
5. Tags are normalised the same way on both sides: trimmed, upper case, double spaces removed, spaces around hyphens removed. Hyphens stay part of the tag. Look-alike dashes pasted from Word (en dash, em dash) are treated as hyphens.

## 7. How the comparison works

- `EXCEL_TAGS` and `REVIT_TAGS` are sets. `MATCHED = Excel ∩ Revit`, `MISSING = Excel − Revit`, `EXTRA = Revit − Excel`.
- For every annotation whose tag is in the Excel, width and height are compared (tolerance 0.5 mm).
- One result row per annotation, plus one row per missing Excel tag.

| Status | Meaning |
|---|---|
| PASS | Tag in Excel, width and height agree |
| MISSING | In Excel, not annotated on any checked view |
| EXTRA | Annotated, not in Excel |
| WIDTH MISMATCH / HEIGHT MISMATCH / WIDTH + HEIGHT MISMATCH | Tag matches, size differs |
| DUPLICATE | Same tag annotated more than once (see below), sizes correct |
| INVALID ANNOTATION | No tag, or no size, could be read |

A row has one status; a mismatch outranks a duplicate, and the Comments column still says it is duplicated.

**Duplicate rule** (drop-down in the window, default *same tag twice in one view*): same tray tagged twice in one view / same tag twice in one view / on one sheet / anywhere in the project. Long tray runs are often tagged several times on purpose, so pick the rule that matches your drafting standard.

**Quantity.** For `2x300x100` the width and height are compared per tray, and the quantity is shown in its own column.

**Text versus model.** When the annotation text and the real tray size differ, the status follows the text (that is what is printed) and the difference is written in Comments.

## 7a. Choosing the sheets to check

By default every sheet is checked. **Choose sheets...** (row 3 of the window) opens a list of all sheets with a tick box each.

Filters, which only narrow the list and never change what is ticked:

- **Search**: words in the sheet number or name.
- **Number series**: the part of the sheet number before its first digit (`E-` for `E-301`).
- **Parameter is value**: any sheet parameter that differs between sheets (Discipline, Zone, Sheet Issue Date, Drawn By, your own shared parameters). The chosen parameter is also shown as a column.
- **Only sheets with cable tray annotations**: available after the first check; the list then also shows how many annotations each sheet had.
- **Only ticked sheets**.

**Tick shown / Untick shown / Invert shown** act on the filtered list. Highlight several rows and press Space to tick them together. **All sheets** in the main window goes back to the whole project.

When only some sheets are checked, **MISSING means "not on the checked sheets"**: the tag may be annotated on a sheet you left out. The Comments column, the status bar and the report's Summary sheet all say how many sheets were checked. A view placed on several sheets is reported under the sheet you ticked.

The selection lasts while the window is open; it is not saved between sessions.

Files: `sheets.xaml` (the dialog) and `sheet_picker.py` (its logic, no Revit access).

## 7b. Checking the cable tray elements (tag in a parameter)

Row 2 of the window, **Check**, chooses what is compared with the Excel:

- **Cable trays in the model** (the default): every cable tray element in the model. Sheets, views and annotations play no part, and their options are hidden.
- **Annotations on sheets**: the cable tray tags printed on the sheets (sections 5 to 7a).
- **Both**.

The last choice is remembered. The default for a first use is `CHECK_MODE` in `config.py`.

- **Tag**: read from a parameter of each cable tray (instance first, then type).
- **Tag parameter**: *(auto-detect)* uses every text parameter in which at least one tray holds a tag from the Excel, and the status bar says which parameter that was. Or pick the parameter yourself from the list.
- **Size**: the tray's real Width and Height.
- **Grouping**: a run is many segments with the same tag, so there is one row per tag and size, with the number of segments in Comments. A run drawn in two sizes gives two rows, and the wrong one is a mismatch. Segments sharing a tag are never reported as DUPLICATE.
- **EXTRA**: a tag on a tray that is not in the Excel. **MISSING**: an Excel tag on no tray.
- **Trays with no tag** are only counted in the status bar. Tick *List cable trays that have no tag* to list them as INVALID ANNOTATION, grouped by size.
- The tray check covers the whole model and ignores the sheet selection. Rows show `(model element)` instead of a view; **Open View** lets Revit find a view that shows the tray, and **Highlight Errors** selects every segment of the run.
- Only cable trays are read, not cable tray fittings (fittings have no width / height).

If you pick a parameter that holds something else (for example Comments), its values appear as EXTRA tags, which makes a wrong choice obvious.

## 8. Highlighting and navigation

- **Highlight Errors** selects the error annotations and their cable trays. It prefers the errors in the active view (or on the active sheet); if none are there, it selects all of them. Missing tags have no element and are only listed.
- **Clear Highlights** clears the selection.
- **Open Sheet** (or double-click a row) opens the sheet and selects the annotation. **Open View** opens the model view and zooms to it.
- Nothing is written to the model: no overrides, no parameters, no transaction.

The window is modeless, so Revit stays usable while it is open. Fix a tag, click **Run check** again; the Excel is re-read too.

## 9. Exporting the report

**Export QA Report** writes an `.xlsx` with:

- **QA Report**: Status, Calculation_Tag, Sheet Number, Sheet Name, View Name, ElementId, Excel Width, Excel Height, Revit Width, Revit Height, Description, Comments, Annotation ElementId. Frozen header, filter buttons, status cells coloured (PASS green, MISSING and mismatches red, EXTRA orange, DUPLICATE yellow, INVALID purple).
- **Summary**: the counts, the model, the Excel file and the date.

ElementId is the cable tray's id when the annotation is a tag, otherwise the annotation's id.

## 10. Error handling

- Excel problems (wrong type, missing column, no rows, file not found) are shown as messages that say what to fix.
- A report that cannot be written because it is open in Excel gives a message, not a traceback.
- Orphaned tags, deleted elements and unreadable parameters are skipped or reported as INVALID ANNOTATION; one bad element never stops the scan.
- If you switch to another model while the window is open, the highlight and open actions refuse and tell you why; **Run check** then checks the model that is active.

## 11. Performance

- Sheets and viewports are indexed once (two collectors). Each reader makes one pass over the document. Nothing is collected per sheet.
- View names are cached. The comparison uses sets and dictionaries and is linear: 5,000 Excel tags against 5,000 annotations compare in well under a second in the test run.
- The result grid is virtualised.

## 12. Testing procedure

Do this once on a copy of a real project before relying on the tool.

1. Make a small Excel with five tags you can see on one sheet.
2. Run the tool. Expect five PASS and no errors, apart from EXTRA for trays not in your small list.
3. Change one width in the Excel → that row becomes WIDTH MISMATCH. Change a height → HEIGHT MISMATCH.
4. Add a tag to the Excel that is not on any sheet → MISSING.
5. Tag one tray twice in the same view → DUPLICATE.
6. Select an error row → **Open Sheet**: the right sheet opens and the tag is selected.
7. **Highlight Errors**, then **Clear Highlights**.
8. **Export QA Report** and check the colours and the Summary sheet.
9. Confirm the model is unchanged: the Undo list has no new entry and the file is not marked as modified.

If step 2 shows INVALID ANNOTATION for every tag, your tag family's label does not hold the text the tool expects: see section 5, points 2 and 4.


## 7c. Trays not tagged on a sheet, and colours in the model

**Flag trays that are not tagged on a sheet** (CABLE TRAYS row, on by default).
A cable tray run that has a calculation tag in its parameter but carries no tag
on any checked sheet gets the status `NOT TAGGED ON SHEET`.

- One tagged segment is enough for the whole run (same calculation tag).
- A tag counts when it points at a segment of the run, or when an annotation
  shows the calculation tag. A tag in a view that is on no sheet does not count.
- A tray that is also the wrong size keeps its mismatch status; its Comments
  say "Also not tagged ...".
- In "Cable trays in the model" mode the tags are read for this purpose only
  and are not listed. Use **Choose sheets** to limit which sheets count.

**Colour by issue** colours, in the ACTIVE Revit view, the elements of the rows
that are shown in the list, one colour per kind of issue (the MODEL COLOURS
chips; click a chip to show only that kind). The colours are set in
`config.py` (`MODEL_COLOURS`).

This is the only action that writes to the model: per-element view overrides
in one transaction. **Remove colours** or Ctrl+Z takes them off. Remove colours
also clears any other per-element override those elements had in that view.
