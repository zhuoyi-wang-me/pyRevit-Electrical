# ElectricalAutomation.extension — Auto-Place Fixtures (v6)

A pyRevit extension that places **electrical, mechanical and plumbing** families (sockets, switches, data outlets, isolators, fan coil units, sinks, water heaters…) on the walls of rooms read from a **linked architectural model**. Fixtures are set at user-defined heights above Finished Floor Level and face into each room. Everything runs from one window with a live preview, and a full report is produced afterwards.

**New in v6 – write data into the placed fixtures**

Every rule has a *Write data to the placed fixtures* section. Choose a fixture parameter (the list shows the family's instance parameters and the project/shared parameters bound to its category, or you can type a name) and a value, then click **Add line**. Each line is `Parameter = text with {tokens}`:

```
Comments        = Serves {Equip:Mark} - Room {Room Number}
EL_Room Number  = {Room Number}
EL_Room Name    = {Room Name}
EL_Description  = Fused spur for {Equip Type} on {Equip:System Name}
EL_Status       = NEW
```

| Tokens | Value |
|---|---|
| `{Room Number}` `{Room Name}` `{Room Level}` `{Room:<parameter>}` | The linked room the fixture is in, including any room parameter (e.g. `{Room:Department}`) |
| `{Equip:<parameter>}` | Any instance or type parameter of the served MSFD / FCU / equipment, including linked models (e.g. `Mark`, `Type Mark`, `System Name`, `Size`) |
| `{Equip Family}` `{Equip Type}` `{Equip Id}` `{Equip Model}` | Served unit's family, type, element id, source model |
| `{Fixture Type}` `{Date}` | The placed type and today's date |
| plain text | Written as typed, e.g. `NEW` |

* **Check before placing.** Values are resolved during Preview; the *Data (first fixture)* column shows a sample for each room or unit.
* **Missing values.** Tokens with no value (e.g. an empty Mark) are written as empty text and counted in the summary.
* **Parameter types.** Text parameters are written directly. Number, length and Yes/No parameters are converted from the text.
* **Report.** After placement, the report lists how many fixtures each parameter was written to, plus any failures (parameter missing on the family, read-only, invalid value). A failed value never removes the fixture.
* **Rule type check.** Equipment tokens need an equipment rule; room-wall rules accept room tokens and fixed text.

**Added in v5 – clash avoidance**

New fixtures are kept clear of MSFDs, duct and pipe accessories, mechanical and plumbing equipment, air terminals, specialty equipment and existing electrical items, whether they are in this project or in any loaded link. Ducts, pipes, cable trays and conduits can be included optionally. Settings are on the Options tab under *Clash avoidance*.

* **Real sizes are used.** The fixture's size comes from its family type geometry, plus a clear gap (default 50 mm). Obstacles are measured along their own axes, so a rotated MSFD isn't treated as a large square. Ducts and pipes become a box along their centre line using their real width, height or diameter.
* **Clashing fixtures slide to a clear spot.** A clashing fixture slides along the wall (or along the unit, for on-equipment rules) in 50 mm steps, up to 1000 mm, moving *away* from the unit it serves first. The Preview notes the result, e.g. *"moved 150 mm along wall to avoid MSFD [25109867] (…MEP…)"*. If there's no clear spot within the limit, it is skipped and the reason is given.
* **New fixtures avoid each other** as well as existing elements. The unit a fixture is hosted on is never counted as a clash.
* **All modes are covered,** including ordinary room sockets, which will no longer land behind equipment.

**Added in v4 – X / Y / Z offsets for mechanical, plumbing and accessories**

Both equipment modes now take full X/Y/Z offsets for every source category: Mechanical Equipment, Plumbing Fixtures, Plumbing Equipment, Duct Accessories (MSFD, VAV, dampers) and Pipe Accessories.

| Mode | X / Y | Z |
|---|---|---|
| **On wall next to equipment** | Pick the direction: **Along the wall** (X slides the fixture right/left as seen from the room); **Equipment axes** (X = unit's right, Y = unit's front, then snapped to the nearest wall, so it follows rotated units); or **Project axes** (X = east, Y = north, then snapped) | The height field: above FFL, or relative to the unit's elevation |
| **On the equipment** | X = unit's right, Y = unit's front, measured from the centre of the chosen side | Z = unit's up. The offset component pointing out of the chosen face is a stand-off, applied to level-based types only (face-hosted fixtures stay on the face) |

**Automatic offset from the unit edge.** Set *Measure offsets from* to **Unit edge**. The tool then reads each unit's real size from its geometry, so the offset is the clear gap from the unit's end rather than from its centre. X = `100` on an 826 mm MSFD puts the fixture 413 + 100 = 513 mm from the damper centre, and every other damper size is handled automatically. A positive X uses the right end and a negative X the left end. If that end is blocked by a corner or door clearance, the other end is used. With *height from equipment*, Z is measured from the top (+) or bottom (−) of the unit. The Preview note shows the unit size and resulting offset for each item so you can verify it.

Examples: *spur 300 mm to the right of each MSFD at 1200 AFF* → Along the wall, X `300`, height from FFL `1200`. *Isolator on each FCU front, 200 mm to the unit's right and 50 mm down* → On the equipment, side Front, X `200`, Y `0`, Z `-50`. Templates from v3 are converted automatically and keep the same positions.

**Added in v3**
* **On-equipment hosting.** Isolators and control boxes can be placed directly *on* FCUs, BCUs and other units: choose the side (front / back / left / right / top / underside) and two offsets from its centre. Face-based types are hosted on the equipment face, in this project or a linked MEP model. Level-based types are placed at the same point.
* **Duct and pipe accessories** as sources. Pick MSFDs, VAVs and motorised valves by family/type name (e.g. `MSFD`) and place a box on the **nearest wall**.
* **Height from the equipment** for nearest-wall rules (e.g. `0` = level with the damper), as well as height above FFL.
* **Better room matching.** Units in ceiling voids match the room below, dampers sitting inside a wall match an adjacent room, and with *All rooms* the on-equipment rules also cover units outside any room (plant rooms, voids).

**Added in v2**
* A single tabbed window (Rules / Preview / Options) replaces the chain of dialogs.
* **Room-size quantities**: 1 per length of wall, 1 per floor area, or area bands (e.g. `10:2, 25:4, 50:6, *:8`), with optional min/max per room. Bigger rooms automatically get more sockets.
* **More categories**: Electrical/Lighting Fixtures and Devices, Electrical Equipment, Mechanical Equipment, Mechanical Control Devices, Plumbing Fixtures, Plumbing Equipment (2024+).
* **Next-to-equipment rules**: put a socket, isolator or fused spur on the nearest wall beside every Mechanical Equipment / Plumbing Fixture / Plumbing Equipment item, whether it's in this project, the architectural link or any loaded link.
* JSON rule templates, plus automatic restore of your last rule set.
* Wall offset for level-based families, and height 0 for floor-standing equipment.

Targets **Revit 2023, 2024, 2025 and 2026** under pyRevit's IronPython 2.7 engine.

---

## 1. Folder structure

```
ElectricalAutomation.extension/
├── README.md                                  <- this guide
├── lib/
│   └── elecplace/                             <- shared library (auto-added to sys.path by pyRevit)
│       ├── __init__.py
│       ├── compat.py      Revit 2023-2026 compatibility, units, names
│       ├── mathutil.py    pure-python interval math + spatial hash (unit-tested)
│       ├── settings.py    persistent options (pyRevit config)
│       ├── linkdata.py    link instances + room extraction
│       ├── geometry.py    boundary -> wall segments, face lookup, openings
│       ├── planner.py     rules, family validation, duplicates, dry-run plan
│       ├── executor.py    transactions + NewFamilyInstance
│       ├── ui.py          main window logic (pyrevit.forms.WPFWindow)
│       ├── ui_main.xaml   main window layout (WPF)
│       ├── equipgeom.py   equipment frame, side/offset points, face hosting
│       ├── clash.py       obstacle index (host + links), oriented-box clash checks
│       ├── datamap.py     parameter data templates: room / equipment tokens, writing
│       └── report.py      output-window report + CSV
├── ElecAutomation.tab/                        <- ribbon tab "ElecAutomation"
│   ├── bundle.yaml
│   └── Fixtures.panel/                        <- ribbon panel "Fixtures"
│       └── AutoPlaceFixtures.pushbutton/      <- the command button
│           ├── bundle.yaml    title / tooltip
│           ├── icon.png
│           ├── script.py      main workflow (click)
│           └── config.py      settings dialog (Shift+Click)
└── tests/
    ├── test_mathutil.py       run outside Revit: python tests/test_mathutil.py
    ├── test_equipgeom.py      side/offset geometry check (XYZ mock)
    ├── test_clash.py          MSFD / FCU clash + slide scenario
    └── test_datamap.py        parameter data templates
```

---

## 2. Installation

**Prerequisites**

1. Revit 2023, 2024, 2025 or 2026.
2. pyRevit installed, using a release that officially lists your Revit version as supported (Revit 2025/2026 need the recent 5.x line because of the .NET 8 runtime). Check with `pyrevit env` in a command prompt.
3. The default **IronPython** engine (no CPython flag is needed).

**Option A — local folder (recommended for testing)**

1. Copy the whole `ElectricalAutomation.extension` folder to a location such as
   `C:\BIM\pyRevitExtensions\ElectricalAutomation.extension`
   (the folder name must end in `.extension`).
2. In Revit: **pyRevit tab ▸ Settings ▸ Custom Extension Directories ▸ Add folder** and select `C:\BIM\pyRevitExtensions` (the *parent* folder).
3. Click **Save Settings and Reload**.
4. A new **ElecAutomation** tab appears with the **Auto-Place Fixtures** button.

Command-line alternative: `pyrevit extensions paths add "C:\BIM\pyRevitExtensions"` then `pyrevit reload` (or restart Revit).

**Option B — company deployment via Git**

```
pyrevit extend ui ElectricalAutomation https://<your-git-server>/ElectricalAutomation.git --dest="C:\BIM\pyRevitExtensions"
```

Users receive updates through **pyRevit ▸ Update**.

**Verify**

Run `python tests/test_mathutil.py` (any Python) to check the placement math, then run the button on a test project with a linked architectural model.

---

## 3. Using the tool

1. Load the families you want to place into the MEP project. Face-based families are best for wall devices. Most mechanical/plumbing equipment is level-based; for those, tick **Options ▸ Allow level-based families**.
2. Make sure the architectural model is **linked and loaded**.
3. Click **Auto-Place Fixtures**. One window opens.

**Header.** Pick the architectural link. The eligible room count updates immediately.

**Tab 1 – Rules.** Build rules in the editor on the right, then click **Add rule**. Select a rule in the list to edit it, then click **Update selected**. You can also Duplicate, Delete and Move rules up or down. Rules run top to bottom, and later rules avoid positions already used by earlier ones.

| Editor field | Meaning |
|---|---|
| Placement | **On room walls** · **On wall next to equipment** (plumbing, MSFD…) · **On the equipment itself, with offset** (FCU / BCU…) |
| Category / Family type | Any loaded type in the supported categories. Tags show the hosting: `[FACE]`, `[LEVEL]`, `[LEVEL - enable option]`, `[NOT SUPPORTED]` |
| Height above FFL | Any Revit length syntax (`450`, `450mm`, `1.2 m`, `1'-6"`). Use `0` for floor-standing units. Smart defaults: switches/isolators 1200, sockets/data 450 |
| Offset from wall | Level-based only: pushes the insertion point into the room (useful when a unit's origin is at its centre) |
| Quantity rule (walls) | **Fixed** per room · **1 per wall length** (e.g. 3600 mm) · **1 per floor area** (e.g. 10 m²) · **Area bands** `10:2, 25:4, 50:6, *:8` · plus optional **min / max** |
| Equipment to serve | Mechanical Equipment / Plumbing Fixtures / Plumbing Equipment / Duct Accessories / Pipe Accessories; *family/type name contains* filter (e.g. `MSFD`, `FCU, BCU`); source models; max. distance to wall; fixtures per equipment |
| Height measured from | Nearest-wall rules only: FFL, or the equipment elevation plus an offset (negative = below) |
| Position on the equipment | On-equipment rules: side + Offset right + Offset up, measured from the centre of that side as seen from outside. On top/underside: right + towards front/back |
| Target rooms | All · levels · name contains · number starts with · picked list. Shows a live "Matches N rooms" count |

**Tab 2 – Preview.** Click **Preview** in the footer. You'll see one row per room (or per equipment item) with its size basis (area and wall length), target quantity, planned quantity and notes, plus a list of every skip and shortfall. Nothing is changed yet.

**Tab 3 – Options.** Clearances, duplicate radius, finish vs. centre boundary, base offset, level-based toggle, schedule level, CSV and comments tag. **Save as my defaults** stores them.

**Footer.** Load/Save template (JSON), Preview, **Place fixtures** (asks once more, then closes and places), and Cancel.

After placement, the pyRevit output window shows the report. Element ids are clickable, and a CSV copy is saved to `%TEMP%\ElecAutoPlace\`. The whole run is **one Undo** (`Ctrl+Z`). Your last rule set is restored automatically from `%APPDATA%\pyRevit\ElecAutoPlace\last_rules.json`.

### Typical MEP set-ups

| Need | Rule |
|---|---|
| Isolator on each FCU, front face, 150 mm right of centre | *On the equipment* · Mechanical Equipment · name `FCU` · side Front · offsets `150` / `0` |
| Box on the underside of each BCU | *On the equipment* · name `BCU` · side Bottom · offsets as needed |
| Control/junction box on the wall beside every MSFD, level with it | *On wall next to equipment* · Duct Accessories · name `MSFD` · height from Equipment elevation `0` · max distance `1500` |
| Fused spur beside water heaters at 1200 AFF | *On wall next to equipment* · Plumbing Equipment/Fixtures · name `heater` · height from FFL `1200` |

**Equipment in this project or in a linked model.** On some projects the FCUs, BCUs, MSFDs and plumbing items are modelled in the electrical project itself; on others they are in a linked MEP model. Set **Equipment from** per rule:

| Option | Use when |
|---|---|
| Auto – this project + all loaded links (default) | You don't know or it varies. Everything is searched, and a unit reached through two sources is kept only once |
| This project only | Equipment is modelled in the open project |
| All linked models only | Equipment is in one or more linked MEP models |
| Linked model: *name* | One specific linked model (every placed instance of it is included) |

Both cases are placed the same way. Nearest-wall fixtures always host on the architectural link's walls. On-equipment fixtures host on the unit face with a plain reference (this project) or a link reference (linked model), so they move with the unit either way. The Preview and the report show how many items were found in each model (e.g. *24 found – 0 in this project; 24 in MEP_Model.rvt*), so a wrong source is obvious before anything is placed. Templates store linked models by name. If that model isn't loaded in another project, the rule is flagged when the template is loaded. Nested links (a link inside a link) are not searched.

**On-equipment hosting needs a 3D view.** Face hosting finds the equipment face with a ray cast (`ReferenceIntersector`) in a non-template 3D view without a section box, preferring `{3D}`. The equipment and its linked model must be visible in that view. If a unit is hidden or an offset runs off the face, it is listed in the Preview with the reason.

### Room-size quantity examples (metric project)

| Rule | 9 m² room, 12 m wall | 30 m² room, 22 m wall | 80 m² room, 36 m wall |
|---|---|---|---|
| Fixed `2` | 2 | 2 | 2 |
| 1 per `3600` mm wall | 4 | 7 | 10 |
| 1 per `10` m² | 1 | 3 | 8 |
| 1 per `10` m², min 2, max 6 | 2 | 3 | 6 |
| Bands `10:2, 25:4, 50:6, *:8` | 2 | 6 | 8 |

Wall length means the room boundary that is backed by usable (straight, non-curtain) walls. Separation lines and curtain walls don't count.

## 4. Placement algorithm

**A. Room acquisition (link coordinates).** All `OST_Rooms` elements of the link document are collected. A room is valid only if `Location != null` (placed), `Area > 0` (enclosed, not redundant), `GetBoundarySegments()` returns loops, and at least one boundary segment's `ElementId` is a `Wall`. Everything else goes to the "skipped" list with a reason.

**B. Boundary → host wall segments.** For each `BoundarySegment` (finish-face boundary by default):
* rejected if it comes from a nested link, is not a wall (separation line, column), is a curtain wall, is curved, or is shorter than the minimum length — each rejection is counted for the report;
* the **inward direction** is the left normal of the segment (Revit returns outer loops counter-clockwise), then **verified** with `Room.IsPointInRoom` on a probe point 150 mm inside, and flipped if needed. Loop orientation is therefore never trusted blindly;
* the **usable length** is `[corner clearance, length − corner clearance]` minus exclusion zones around every door/window hosted in that wall (projected onto the segment, width + clearance). Pieces shorter than the minimum are dropped.

**C. Distribution.** All usable pieces of the room are chained into one "unrolled" perimeter of length *T*. For *N* fixtures, point *i* sits at `T·(i + 0.5)/N` — the centre of *N* equal bays — so fixtures spread around the room instead of bunching on the first wall, and never land in corners or openings.

**D. Face resolution and height.** For each slot, the probe point is `FFL + mounting height`, where FFL is the room level's `ProjectElevation` (internal-origin based, independent of the level's Elevation Base), optionally plus the room's Base Offset. The wall's side faces (`HostObjectUtils.GetSideFaces`, cached per wall) are filtered to faces whose normal points into the room (dot ≥ 0.98), and the probe is projected with `PlanarFace.Project`. A projection outside the face, or at a different Z, means the height is above the wall top or on a split face — the slot is rejected.

**E. Link transformation.** The projected point and face normal are converted with `RevitLinkInstance.GetTotalTransform()` (`OfPoint` / `OfVector`), which handles rotated, moved and shared-coordinate links. The face reference is converted with `Reference.CreateLinkReference(linkInstance)` so the host can reference geometry inside the link.

**F. Duplicate avoidance.** Existing fixtures of the same categories in the host, plus every fixture planned earlier in the run, are stored in a hashed 3D grid. A slot within the duplicate radius is slid along its usable piece (±1, ±2, ±3 steps). If all attempts clash, it is reported as a duplicate.

**G. Orientation.** Face-based placement uses `NewFamilyInstance(linkFaceRef, point, refDir, symbol)` with `refDir = Z × faceNormal`, so the family's normal axis faces into the room and its local Y axis points up (upright device). After regeneration, any instance whose `FacingOrientation` points away from the room is flipped. Level-based fallback instances are rotated about Z by `FacingOrientation.AngleOnPlaneTo(inward, Z)`.

**G2. Room-size quantity.** For "on walls" rules, the target count per room comes from the chosen method: `ceil(wall length / spacing)`, `ceil(area / area per fixture)`, or the first area band whose upper limit the room area does not exceed. The result is then clamped to min/max. That count goes into the even distribution in step C.

**G3. Next-to-equipment.** Equipment instances of the ticked categories are collected from the chosen sources (host `Transform.Identity`, or each link's total transform), skipping nested sub-components. Each insertion point is converted into architectural-link coordinates with `linkTransform.Inverse`, then matched to a target room (bounding-box prefilter, level/height check, then `Room.IsPointInRoom`). The nearest point on that room's usable wall intervals is found, and the equipment is skipped with a reason if that point is beyond the max distance. *k* fixtures are spaced around that point, and each goes through steps D–G (face lookup, transform, duplicate slide, orientation).

**G4. On the equipment.** For each matching unit, the tool builds its local frame from `GetTransform()`, `FacingOrientation` and `HandOrientation`, and measures the real extents of its solid geometry along those axes, so rotated or mirrored units and links are handled. The centre of the chosen side is shifted by the two offsets (several fixtures per unit are spaced along offset 1). For face-based types, a ray is cast from 600 mm outside back onto the unit with `ReferenceIntersector` (`FindReferencesInRevitLinks = true`). The hit must belong to that unit and lie within 300 mm of the expected face. The returned reference (a link reference for linked units) and hit point feed `NewFamilyInstance(reference, point, offset-1 axis, symbol)`, which keeps the fixture upright on vertical faces. Level-based types are placed at the point and rotated to face outwards.

**H. Transactions.** One `TransactionGroup` (single Undo) → one `Transaction` per room (with an `IFailuresPreprocessor` that removes warnings) → one `SubTransaction` per fixture. A failing fixture never rolls back its neighbours, and a fatal error rolls back the whole group.

### Mechanical and plumbing families

Most mechanical equipment and plumbing fixtures are **level-based** (`OneLevelBased`). With *Allow level-based families* ticked, they are placed on the host level nearest below the target height, at the wall face (plus *Offset from wall*), with *Elevation from Level* set to the height and a rotation so the family's front faces into the room. Face-based versions are hosted exactly like electrical devices. **Wall-hosted** plumbing templates (many wall-hung WCs and basins) cannot be hosted on linked walls, as explained below. Use face-based or level-based versions of those families.

### Why "wall-hosted" means face-based here

Revit **cannot host legacy wall-based families (`OneLevelBasedHosted`) on walls inside a linked model**; this is a platform limitation, not a script limitation. The supported, industry-standard approach is **face-based** families hosted on the linked wall face through a link reference, which is exactly what this tool does. Wall-hosted types are detected during validation and reported with an explanation. Face-based instances hosted this way keep their association and move with the link face (Revit may flag them as orphaned if the host wall is later deleted in the architectural model).

---

## 5. Validation & error handling summary

| Check | Where | Result |
|---|---|---|
| Project (not family), not read-only | `script.py` | alert + exit |
| Loaded links present | `linkdata.get_loaded_links` | alert + exit, unloaded count shown |
| Room placed / enclosed / has walls | `linkdata.collect_rooms` | skipped with reason |
| Family type still loaded | `planner.classify_symbol`, `executor._place_one` | rule rejected / error |
| Family hosting behaviour | `planner.classify_symbol` | face-based OK, others explained |
| Height valid & within max | `ui.ask_height` | re-prompt |
| Height above wall top / no face | `planner._resolve_slot` | slot skipped with reason |
| Duplicate location | `planner._resolve_slot` + `GridIndex` | nudged, else skipped |
| Instance creation failure | `executor` SubTransaction | rolled back individually, reported |
| Unexpected exception | `script.py` top level / TransactionGroup | logged, rolled back |

Logging uses `script.get_logger()`; **Ctrl+Click** runs pyRevit in debug mode to show `logger.debug` output and tracebacks.

---

## 6. Revit API reference list

| Area | API members |
|---|---|
| Links | `RevitLinkInstance.GetLinkDocument`, `RevitLinkInstance.GetTotalTransform`, `Transform.OfPoint`, `Transform.OfVector`, `Transform.Inverse`, `Transform.Identity`, `Reference.CreateLinkReference` |
| Categories | `BuiltInCategory` (electrical, lighting, devices, mechanical, plumbing), `Category.IsBuiltInCategoryValid`, `ElementMulticategoryFilter` (BuiltInCategory and ElementId overloads) |
| Equipment | `FamilyInstance` (`SuperComponent`, `GetTransform`, `FacingOrientation`, `HandOrientation`, `get_Geometry`), `Options`, `GeometryInstance.GetInstanceGeometry`, `Solid`, `Edge.AsCurve`, `LocationPoint`, `Element.get_BoundingBox`, `Room.UnboundedHeight` |
| Face hosting on equipment | `View3D` (`IsTemplate`, `IsSectionBoxActive`), `ReferenceIntersector` (`FindReferencesInRevitLinks`, `Find`), `FindReferenceTarget.Face`, `ReferenceWithContext` (`GetReference`, `Proximity`), `Reference.LinkedElementId` |
| Rooms | `Architecture.Room` (`Number`, `Level`, `Area`, `Location`, `BaseOffset`, `GetBoundarySegments`, `IsPointInRoom`), `SpatialElementBoundaryOptions`, `SpatialElementBoundaryLocation`, `BoundarySegment` (`ElementId`, `LinkElementId`, `GetCurve`) |
| Walls & faces | `Wall` (`Width`, `WallType.Kind`), `WallKind`, `HostObjectUtils.GetSideFaces`, `ShellLayerType`, `Element.GetGeometryObjectFromReference`, `PlanarFace` (`FaceNormal`, `Project`), `IntersectionResult` |
| Openings | `FamilyInstance.Host`, `LocationPoint`, `ElementMulticategoryFilter`, `BuiltInParameter.DOOR_WIDTH / WINDOW_WIDTH / FAMILY_WIDTH_PARAM` |
| Families | `FamilySymbol` (`IsActive`, `Activate`, `FamilyName`), `Family.FamilyPlacementType`, `FamilyPlacementType`, `Document.Create.NewFamilyInstance` (face & level overloads), `FamilyInstance` (`FacingOrientation`, `CanFlipFacing`, `flipFacing`), `ElementTransformUtils.RotateElement`, `StructuralType` |
| Transactions | `TransactionGroup` (`Assimilate`, `RollBack`), `Transaction`, `SubTransaction`, `FailureHandlingOptions`, `IFailuresPreprocessor`, `FailuresAccessor`, `FailureSeverity`, `TransactionStatus` |
| Units & ids | `UnitUtils`, `UnitTypeId`, `SpecTypeId`, `UnitFormatUtils.Format / TryParse`, `LabelUtils.GetLabelForUnit`, `ElementId.Value` (2024+) / `IntegerValue` (2023) |
| pyRevit / .NET | `forms.WPFWindow` (XAML), `System.Data.DataTable`, `forms.save_file`, `forms.pick_file`, `revit.doc`, `forms.SelectFromList`, `forms.CommandSwitchWindow`, `forms.ask_for_string`, `forms.alert`, `script.get_output` (`print_md`, `print_table`, `linkify`), `script.get_logger`, `script.get_config`, `script.save_config` |

**Version notes:** `ElementId.IntegerValue` was removed in Revit 2026, so `compat.eid_int()` prefers `.Value`. Only ForgeTypeId unit APIs are used (no `DisplayUnitType`). No `f-strings` or Python 3 syntax are used, for IronPython 2.7.

---

## 7. Suggested future enhancements

1. **Excel / CSV integration.** Read a room-number → rule matrix from Excel so the engineer's schedule drives placement, and export the report to `.xlsx`.
2. **Room-type presets.** Map room names or a room parameter (Department, Occupancy) to templates automatically, e.g. kitchens get counter-height sockets, toilets get shaver points.
3. **Door-relative switches.** Latch-side placement using `FamilyInstance.HandOrientation` and door width.
4. **Counter / casework awareness.** Read linked casework to set backsplash heights and avoid placing behind cabinets.
5. **Equipment connector matching.** Pick the socket type from the equipment's electrical connector (voltage/load), and create the `ElectricalSystem` connection automatically.
6. **Circuiting.** Group placed fixtures per room into circuits by load/quantity and assign to a panel.
7. **Update/sync mode.** Use the Comments tag or extensible storage to move/remove fixtures when the architectural model changes.
8. **Curved walls** via arc-length distribution and cylindrical face normals.
9. **Clash with linked structure/MEP** using `ElementIntersectsSolidFilter` around each planned location.
10. **Visual preview** by drawing temporary DirectShape markers in a 3D view before committing.
