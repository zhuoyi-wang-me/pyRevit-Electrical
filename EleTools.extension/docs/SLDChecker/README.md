# SLD Checker

**Ribbon:** Electrical tab → Single Line panel → **SLD Checker**.
**Read-only:** it never opens a transaction and never changes the model.

## Run it
1. Open the project model in Revit 2025 or 2026. If the button is missing, use pyRevit → Reload.
2. Click **SLD Checker**. The setup window opens with the template you used last.

| Tab | What you set |
|---|---|
| **1 Scope & Checks** | Which views to check: all views with Detail Items, the active view, or picked views (with a filter). Tick the checks you want reported, and choose whether INFO rows are shown. Duplicate and ambiguous names are always reported. |
| **2 SLD Parameters** | One row per field (panel name, fed from, feeds, breaker rating, poles, cable size, ...). The drop-down lists the parameters actually found on your Detail Items. The status column shows live results: *✓ value on N elements, e.g. '63A'*, *⚠ exists but empty* or *✗ not found*. **Auto-detect** fills in any field that doesn't find a parameter yet. Comma-separated names are tried in order, for mixed families. |
| **3 Model Parameters** | Equipment, circuit and fixture parameters. The defaults are Revit built-ins (`bip:...`). |
| **4 Recognition & Options** | Family keywords that mark a symbol as a cable, breaker or board, the spare keywords, the TX / SMDB / MDB / DB rules, the tolerances, and the unit used for SLD load values with no unit. |

3. **Template** bar:
   - **Save**: store the current settings in the selected template.
   - **Save As...**: store them under a new name.
   - **Delete**: remove the template.
   - **Import... / Export...**: share a template's JSON file with the team.

   Templates are stored in `%APPDATA%\EleTools\SLDChecker\templates`. Shift+Click the button to open that folder. `<Default>` is built in and cannot be changed.
4. Click **Run Check**. The **QA/QC results window** opens, in the same style as Cable Tray QA/QC. It is modeless, so you can keep working in Revit while it's open.
   - **Header and Run card:** the template, the scope, any linked models, the mapping coverage (which SLD fields were not found), and warnings such as "model not circuited".
   - **Tiles:** SLD boards and feeders, model boards and circuits, boards and feeders matched, Missing, Duplicate / ambiguous, Warnings, Info, and Overall errors. Clicking a tile filters the list.
   - **Check chips** (1. Matching … 6. Loads) are red when the group has errors, orange for warnings only and green when clean. Clicking a chip shows only that group.
   - **Filters:** by severity and by issue type, plus a search box.
   - **Show on SLD** (or double-click a row) opens the SLD view, selects the symbols and zooms to them. **Show in model** selects and zooms to the model elements. **Highlight shown errors** selects every element of the error rows that are shown. These are selection only; nothing in the model is changed.
   - **Export CSV** exports the rows that are shown. **Open log** opens `last_run.txt`.
   - If the window can't open, the classic report in the pyRevit output window is used instead.
6. Every run, including a failed one, also writes `%APPDATA%\EleTools\SLDChecker\last_run.txt`. It holds the header, the counts, the mapping used and found, the number of rows per issue type, every result row, and the traceback on failure. Send this file when reporting a problem.

**Multi-model projects and multi-section boards** (Options tab):
- *Also match equipment and circuits in loaded Revit links* is on by default.
  - Boards that are drawn on the SLD and exist in a link count as matched.
  - A linked board that is missing from the SLD is only reported when its source board is drawn.
  - Linked ids appear as text, because they can't be selected from the host.
- *Same name + same family close together = one board* is also on by default.
  - In the model, the sections must be within 10 m of each other.
  - On the SLD, they must be in the same view and within 60 mm of each other on paper.
  - These cases are reported as INFO "Board sections". Copies that are further apart are still reported as duplicates.
- Only family Detail Items are read. Filled regions are ignored.
- **Ignore these symbols** (Options tab) lists families that are never boards or feeders. The defaults are panel details, interlocks, legends and key plans.
- **Name endings to ignore**: an SLD name such as `PP-01(E)` with no exact model match is tried again as `PP-01`. A match found this way is always reported as the WARNING "Matched by base name".
- **Unnamed symbols** are grouped:
  - On the SLD: one row per family and view.
  - In the model: one INFO row per type. Battery racks and trenches often have no Panel Name on purpose.
- **A feeder drawn in pieces** (several cable or breaker symbols in one view with identical data) counts as one feeder and is reported as INFO "Feeder sections". Pieces with different data are reported as a duplicate, because they're probably different feeders sharing one name.
- **Unloaded Revit links** are listed in the header, because boards in them show as missing.
- If the model has fewer power circuits than 10 % of its boards, the report header warns that the circuit, breaker and cable checks have nothing to compare.

Names are compared without regard to case, spaces, `-` or `_`, so `MEE-Component` also finds `MEE_Component`.

## What is checked
| Check | Content |
|---|---|
| 1. Matching | Missing model element, missing Detail Item (board or outgoing feeder), duplicate Detail Item (same view), duplicate panel name in the model, ambiguous match (never compared), "closest name" hints |
| 2. Panels & Equipment | Name formatting, equipment name, classification TX / MDB / SMDB / DB, transformer identification |
| 3. Circuits & Connectivity | SLD Fed From vs model Supply From, missing circuit, circuit number, circuit feeding a different load, outgoing feeder not connected to equipment, breaker and cable symbols that disagree |
| 4. Breakers | Breaker rating and frame vs circuit Rating / Frame, rating below the circuit design current, number of poles (4P/TPN = 3), spare identification, spare count per board |
| 5. Cables | Size (mm²), number of cores, cable type tokens, description |
| 6. Loads | Total connected load and maximum demand (kVA, or kW where the model has a true load), demand higher than connected load |

How an SLD feeder is matched: the breaker and cable symbols that share the same *Fed From* and *Feeder* in one view form one feeder. That feeder is matched to the circuit by (source panel, circuit number) when the SLD gives a number. Otherwise it is matched by (source panel, downstream board or load name).

Tests: `py -m unittest discover -s tests/SLDChecker`
