# Cable Tray Layout (pyRevit extension)

Creates one Revit **drafting view per cable tray** from a cable schedule (Excel or CSV).
Each view shows the tray cross-section, every cable as a circle, the spacing between
neighbouring cables, the 20% future space and a summary.

Only detail lines, detail arcs and text notes are created. They are ordinary Revit
elements: you can move, edit or delete them.

Made for Revit 2025 with pyRevit 5 (IronPython 2.7). It also loads in Revit 2024 and 2026.

## Install

1. Keep the folder `CableTrayLayout.extension` where it is (inside `ElePyRevitExtension`).
2. In Revit: **pyRevit tab > Settings > Custom Extension Directories**. The folder
   `...\Documents\Ele\ElePyRevitExtension` is already listed on this computer. On another
   computer, add the folder that contains `CableTrayLayout.extension`.
3. Press **Save Settings and Reload**.
4. The button is at **Electrical tab > Cable Tray panel > Tray Layout**.

## The input file

Excel `.xlsx` / `.xlsm` or `.csv`. Excel does not have to be installed, and the workbook may
be open while you read it. Old `.xls` files must be saved as `.xlsx` first.

| Column | Needed | Notes |
|---|---|---|
| Tray ID | yes | One drafting view per Tray ID. An empty cell means "same tray as the row above". |
| Cable type | yes | Text, shown in the cable list. |
| Main cable outside diameter (mm) | yes | Must be more than 0. |
| ECC cable outside diameter (mm) | no | Empty or 0 = this run has no ECC. |
| Quantity | no | Number of runs of this row. Empty = 1. |
| Tray width (mm) | yes | Taken from the first row of the tray that has it. |
| Tray height (mm) | yes | Same. |
| Number of layers | no | Empty = 1. |

The columns are found by their headings, in any order; "Tray No.", "Cable O.D.", "ECC Dia",
"Tray Depth", "Qty" and similar wordings are understood. A title row above the headings is
allowed. Decimal commas (`62,4`) and a trailing `mm` are accepted.

**Save template...** in the window writes an example CSV with the right headings.

## Use

1. Press **Tray Layout**.
2. **Browse...** to the schedule. For a workbook, choose the **Excel sheet**.
3. Choose the **calculation method**:
   - *Method 1A* (default) - the space between two cables is the bigger of their diameters,
     with half a diameter free at each tray side.
   - *Space = D left / 2 + D right / 2* - no free space at the tray sides.

   In both methods the earth cable is attached to its cable and no space is kept for it.
4. Choose what happens **if the view exists**: update it, make a numbered copy, or skip the tray.
5. The list shows every tray with its cable count, layout width, required width and result.
   Trays with input errors are listed in red and cannot be ticked; the reasons are in the box below.
6. Tick the trays and press **Create views**. The last view opens, zoomed to fit, and a
   report lists what was done.

Nothing is changed in the model until **Create views** is pressed.

## Calculation

**Method 1A** (sample drawing "New Calc Method-1A"):

- Only the phase (main) cables count in the width.
- Space between two neighbouring cables: the bigger of their two diameters.
- Free space at the tray sides: half the first cable's diameter at the left, half the last
  cable's diameter at the right.
- Layout width = left edge space + all diameters + all spaces + right edge space.
- The earth cable lies on the tray bottom, touching its cable on the right. It sits inside the
  space and is not counted.

Example: cables 33.1, 21.9, 21.9, 21.9, 25.0, 33.1, 25.0 give
16.55 + 33.1 + 33.1 + 21.9 + 21.9 + 21.9 + 21.9 + 21.9 + 25 + 25 + 33.1 + 33.1 + 33.1 + 25 + 12.5
= 379.05 mm.

**The second method:** space = `D_left / 2 + D_right / 2` between the phase cables, no edge
spaces, earth cable attached as in 1A. Three 40 mm cables give 200.0 mm, with or without
earth cables.

**All methods:**

- Layout width of the tray: the widest layer
- Future space: `layout width x 0.20`
- Required width: `layout width x 1.20`

Values are not rounded during the calculation; they are shown to one decimal place.

**Layers.** The runs are shared out over the layers in schedule order, as evenly as possible
(the lower layers take the extra ones). A main cable and its ECC always stay in the same layer.
Each layer rests on the tallest cable of the layer below.

**Warnings**, in the window and boxed in the view:
- the required width (with future space) exceeds the tray width
- the cables alone exceed the tray width
- the layers are higher than the tray

## What is in the view

- View name `Cable Tray - <Tray ID>`
- Tray: bottom and both sides, at the given width and height
- Tray height dimensioned at the left
- Cables: one circle each with a centre cross and its diameter on a leader (`Ø33.1`),
  earth cables included
- One dimension chain per layer under the tray: edge space, cable, space, cable ...
  (drawn with lines and text)
- Tray width, cable layout width and required width as dimension lines under the chain
- Warning box (only when needed), summary, cable list

- Cable table: CABLE TAG (the tray, one merged cell) | FED FROM | FED TO | CABLE, with grid lines.
  FED FROM and FED TO come from optional columns of the file ("Fed from", "Fed to").

**View scale** is chosen in the window (1:50 by default; "Automatic" picks 1:1 to 1:50 to suit
the cables). **Text style** is chosen from the text types of the project; no text types are
created. Text is sized on paper, so at 1:50 a 2.5 mm style is 125 mm high in the model and the
annotation is large against the cables. Diameter labels step up where they would overlap.

A file may use the heading "CABLE TAG" for the tray when it has no tray column.

**Updating.** The view stores which elements the tool drew. *Update* deletes exactly those and
draws again; anything you added to the view yourself stays. A view with the same name that was
not drawn by this tool is never changed - a numbered view is created instead.

## Files

```
CableTrayLayout.extension/
  Electrical.tab/Cable Tray.panel/Tray Layout.pushbutton/
    script.py        starts the window
    ctl_window.py    the window
    ui.xaml          its layout
    ctl_input.py     reads and checks the Excel / CSV file
    ctl_calc.py      gaps, widths, future space
    ctl_drawing.py   what is drawn
    ctl_revit.py     drafting view, detail lines, text notes, transactions
```

## Limits

- The spacing dimensions are lines and text, not Revit dimension elements.
- One ECC per run is assumed.
- Each tray is drawn in its own transaction: if one tray fails it is rolled back and the
  others are kept. One Undo removes everything from a run.
