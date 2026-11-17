# Electrical Load Report – v2.1

## Bugs fixed
1. Plain "Number" load parameters were still converted as Revit power units
   (Revit counts Number as measurable): 3000 VA came out as 0.279 kVA.
2. Two panels with the same name (or two unnamed panels, which fall back to the type
   name) were merged into one panel in every sheet and in the Panel Check. Duplicates
   now get their element id appended, e.g. "DB-1 [123456]".
3. "No loads matched" gave no reason when every element raised an error or had no
   load of its own; both are now explained.
4. Circuits with an unreadable number of poles were counted as "phase filtered"
   instead of "number of poles unknown".

# Electrical Load Report – v2

## Bugs fixed
1. Panel names: `FamilyInstance.Name` returns the TYPE name, so panels of the same type
   merged into one sheet and the MDB/SMDB filter never matched. Now uses Panel Name
   (RBS_ELEC_PANEL_NAME) -> Mark -> name.
2. `ElectricalSystem` was imported from `Autodesk.Revit.DB` (wrong namespace); the
   failure was swallowed by a bare `except`, so circuit-based panel discovery never ran.
3. Revit 2026: `ElementId.IntegerValue` and `ElementId(int)` were removed. All ids go
   through `eid_int()`; parameter ids use `ElementId(BuiltInParameter)`.
4. Unit fallback divided by 1000; Revit internal power units need /10,763.9 (x10.76 error).
5. Direct mode pushed unitless "Number" parameters through unit conversion (÷10,764 error).
   Unitless values are now read as VA or kVA per a UI setting.
6. Empty "Number of Poles" read as 0 -> fixture judged non-3-phase without checking connectors.
7. Data / fire-alarm / telephone circuits were treated as power circuits.
8. Active-view scope crashed on schedules / templates.
9. `System.IO.Compression.FileSystem` reference can fail on .NET 8 (Revit 2025+);
   zip is now written with `ZipArchive` and explicit forward-slash entry names.
10. Progress bar never repainted (`UpdateLayout` does not pump messages).
11. Counts were formatted `1.00`; aggregated rows came out in random dict order.
12. Panel list parsed names back out of checkbox text (broke on names containing "  (").
13. Empty exports still wrote a file and reported success.

## Improvements
- Layered code: `elr_revit` (API), `elr_report` (pure Python), `elr_xlsx` (writer).
- Per-load apparent power read from the connector on the matching circuit; true load from
  power factor or circuit TrueLoad/ApparentLoad ratio; circuit-share fallback is flagged.
- Phase filter (3-phase / single-phase / all); Mechanical Equipment & Lighting Fixtures.
- Selection scope expands selected panels and circuits into their loads.
- New sheets: Classification Totals (with % share), Panel Check (vs Revit panel totals),
  Diagnostics (run info, skip reasons, warnings). Bold totals rows on every sheet.
- Natural sort (MDB-2 before MDB-10), Excel filter metadata, unicode-safe text.
- UI: two-pane layout, search hint, auto-detected panel type prefixes, circuit counts per
  panel, Cancel during export, settings remembered per user, open file/folder when done.
