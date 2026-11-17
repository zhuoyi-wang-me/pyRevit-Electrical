# Revizto Clashes

**Where:** EleTools › MEP Tools › **Revizto Clashes** (next to the Coordination stack)
**Revit:** 2025 / 2026 (2023+ works) · pyRevit IronPython 2.7

The tool reads a clash export from Revizto and finds both clashing elements
in the open model and its loaded links. A QA/QC window then lets you select
and zoom to the elements, open a 3D section box around the clash, colour the
clashing elements and export the result to CSV.

## What it changes in the model

| Action | Changes the model? |
|---|---|
| Load the file, match elements, filter, search | No |
| **Select + zoom** (or double-click a row), **Clear selection** | No (selection only) |
| **Colour clashing elements** / **Remove colours** | Yes: per-element graphic overrides in the **active view** only, in one transaction (Ctrl+Z undoes it) |
| **Section box 3D** | Yes: creates the first time, then crops, your own 3D view `Revizto Clashes - <user name>`, in one transaction (Ctrl+Z undoes it) |
| **Export CSV** | No (writes a file) |

Nothing else is ever modified, and element parameters are never touched.

## 1. Export the clashes from Revizto

Revizto offers three ways to get clashes out. Clashes from Clash Automation
become issues, so they leave Revizto the same way issues do.

| Revizto export | Format | Use it? |
|---|---|---|
| **Revizto Workspace › project › Reports › New report › Format: XLS** (sent by e-mail) | Excel `.xlsx` | Yes |
| **Issue Tracker › Export › BCF** | `.bcfzip` / `.bcf` | Yes, recommended: it always carries the element ids |
| Issue Tracker list / any table saved as CSV from Excel | `.csv` | Yes |
| Workspace report Format: PDF | `.pdf` | No (no element ids) |

Navisworks clash reports also work: **XML** (`Report format: XML`) and
**HTML (Tabular)**, with the *Item 1 / Item 2 Element ID* columns ticked.

### Revizto clash automation export (the usual case)

To export a clash test, open **Clash Automation**, select the test and choose
**Export to Excel**. The file is named after the test, for example
`306.STR-Structural Trusses Vs 211.MEP-Electrical Equipment.xlsx`, and has
three sheets:

| Sheet | Used? |
|---|---|
| Clash Tests | No (one summary row per test) |
| Groups | No (one row per clash group, without element ids) |
| **Clashes** | **Yes**, one row per clash, picked automatically |

These columns of the **Clashes** sheet are read:

| Column | Becomes |
|---|---|
| `Authoring tool IDs`, e.g. `6113220:9481762` | Revit element id of **A : B** |
| `#` | Clash id |
| `Clash name`, e.g. `1S0-SFR-HEA vs MEE_ELQ-SMDB` | Title. The two halves are name hints for A and B |
| `Test name` | Test. Its two halves, e.g. *Structural Trusses* / *Electrical Equipment*, are category hints |
| `Parent group #`, `Parent group name` | Group (shown as chips when the file holds one test) |
| `Parent group status`, e.g. `Open (ID 53370)` | Status `Open`, and the issue id goes to the notes |
| `Clashing status` | `Clashing`, or `Resolved` (counted as resolved) |
| `Level`, `Grid` | Level / grid column |
| `Found at` (Excel date) | Date |
| `Coordinates (m)` | Clash point, as written in the export (Revizto / shared coordinates) |

`Revizto IDs` are Revizto's internal object ids, not Revit ids, so they are
ignored. So are `Coordinates (ft)` and the alignment columns.

The name and category hints decide between host and link when the same
element id exists in both, which is common between a STR link and an MEP host.

### Which columns hold the element ids? (other exports)

Revizto does not publish a fixed column list, so the tool recognises columns
**by the words in the header**:

* Element ids of element A / B: `Element ID 1` / `Element ID 2`,
  `Element 1 ID`, `Item 1 Element ID`, `ElementID1`, `Revit ID (A)` / `(B)`,
  `Element A Id`, `Object 1 Id` ...
* One column with both ids (`Element IDs`, `Revit ID`), for example
  `123456; 654321`: the first id is A, the second is B.
* GUIDs: `GUID 1`, `IfcGUID`, `UniqueId A`. Revit UniqueIds and IFC GUIDs
  (22 characters, or the 36-character form) are both matched.
* Optional: `Model 1` / `Item 1 Source File` (model name),
  `Category 1`, `Status`, `Clash Test` / `Stamp`, `Title`, `Distance`.
* The header may sit below title rows. The tool scans the first 30 rows and
  every sheet of the workbook.

**If no element-id column is recognised,** the tool asks you to pick the
column for element A and then for element B.

**In a BCF file** the elements are the *selected components* of each topic's
viewpoint: `AuthoringToolId` holds the Revit element id and `IfcGuid` the IFC
GUID. The test name comes from the topic labels, or from the topic type.

> Old binary `.xls` files (Excel 97-2003) cannot be read without Excel.
> Open the file in Excel and save it as **.xlsx** or **CSV UTF-8**.

See [sample_clashes.csv](sample_clashes.csv) for a minimal file.

## 2. Run the tool

1. Open the Revit model, and load the links whose elements are in the clashes.
2. Click **MEP Tools › Revizto Clashes** and pick the export file. The last
   folder you used is remembered.
3. The window opens:
   * **FILE / COLUMNS / MODELS**: the file, which column was used for what,
     the host model and the loaded links. **NOTE** lists links that are not
     loaded, clashes without ids, and ids found in more than one model.
   * **Tiles** (click one to filter): Clashes, Open, In review, Resolved,
     Both found, One found, Not found, In links, Unresolved.
   * **TESTS** chips: one chip per clash test or stamp. A chip is red when
     its test has unresolved clashes. Click a chip to show only that test.
   * **Show** filters (status group, found), and a search box for id,
     title, element, model and notes.
   * **Grid**: clash id, status, test, title, element A / B (Revit category
     and name), their ids, *found* (`Yes - <model>` / `No`), found
     yes / one / no, distance (in mm when the file gives a unit), and the
     clash centre in metres from the internal origin.
4. Actions:
   * **Select + zoom**, or double-click a row: selects both elements,
     linked ones included, and zooms the active plan, section or 3D view to
     them, with the margin in mm.
   * **Section box 3D**: opens your 3D view `Revizto Clashes - <user>` with a
     section box around the **overlap** of the two elements plus the margin.
   * **Colour clashing elements**: colours the host elements of the rows
     shown in the list (after filters) in the active view. Choose *Colour by
     status* (Open red, In review orange, Resolved green) or *A red / B blue*.
   * **Remove colours**: resets those overrides in the active view.
   * **Export CSV**: the shown rows, UTF-8, ready for Excel.

## How elements are matched

1. For each id the tool looks in the host model and in **every loaded link
   instance**. In Revit 2024+ it uses `ElementId.Value`.
2. Element ids are only unique inside one model, so an id may exist in two
   models. The model name from the export (`ELE_Model.rvt`, also with
   `_detached`, `.nwc` and similar endings) and the category decide which.
   If both still match, the host element is used and the row is marked
   *ambiguous*.
3. A UniqueId or IFC GUID is unique, so those never clash.

| Found column | Meaning |
|---|---|
| `Yes - ELE_Model` | Found in that model or link |
| `Yes (ambiguous)` | The id exists in more than one model; the host element was used |
| `Yes (partly found)` | Some of the side's ids were found, not all |
| `No` | Not in this model or any loaded link (deleted, or the link is not loaded) |
| `No ID` | The export has no id for this element |

## Limits

* **Linked elements cannot be coloured one by one.** Revit only overrides a
  whole link. They are still selected and zoomed to, and the status bar
  counts them.
* A link must be **loaded** for its elements to be found.
* Distance is shown in mm only when the export gives a unit (`-0.052 m`). A
  bare number is shown as written.
* Revit cannot select elements on a sheet. Open a model view before you zoom
  or colour.

## For developers

* Pure logic is in `lib/reviztoclash/` (`parse.py`, `match.py`, `report.py`)
  and has no Revit dependency. `revit_side.py` holds the Revit lookups.
* The button is in `MEP Tools.tab/MEP Tools.panel/Revizto Clashes.pushbutton/`:
  `script.py`, `results_window.py` + `results.xaml` (the modeless window), and
  `revit_actions.py` (ExternalEvent runner, zoom, section box, colours).
* Tests, run from `EleTools.extension`:

  ```bash
  py -m unittest discover -s tests/ReviztoClashes
  ```
