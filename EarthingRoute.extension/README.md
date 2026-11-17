# Earthing Route Generator

pyRevit extension for Revit 2025 (also 2024 / 2026). In the **active Floor
Plan** it draws 2D earthing lines from the selected electrical panels to one
selected earth bar, going round columns, walls and MEP elements of the model
and of its Revit links.

It creates **detail lines only**. No 3D geometry, no wires, conduits, cable
trays or fabrication parts, and no line styles.

## Install

1. pyRevit > Settings > Custom Extension Directories > add the folder that
   contains `EarthingRoute.extension`.
2. Reload pyRevit. The button **Create Earthing Routes** is on the **Electrical** tab, in the **Earthing** panel.

## Use

1. Open a Floor Plan. In any other view the tool stops with a message.
2. **Select Earth Bar**: pick one element (any model category).
3. **Select Panels**: pick electrical equipment, then Finish. Panels that are
   selected in Revit when you click the button are taken over.
4. Choose the **line style** (existing styles only; one with "earth" in its
   name is preselected), the **clearance** and the **grid spacing**.
5. Tick the **obstacle categories**, **Include linked models**, and whether
   routes may **pass through door openings**.
6. **Preview Routes**: the routes appear as magenta lines. The window stays
   open, so you can pan and zoom to check them.
7. **Create Routes** draws them in the chosen line style, or **Clear Preview**
   removes them.

The table shows SUCCESS / FAILED per panel with length and number of bends;
the text box holds the same as a report that can be copied.

## What to know

- **Preview lines are real detail lines** (Revit has no other way to show a
  line in a view). They are magenta, tagged as preview, and removed by Clear
  Preview, Create Routes, closing the window, or the next Preview.
- **Which floor counts**: host elements are the ones the plan shows (view
  range and visibility settings). Linked elements likewise on Revit 2024+; on
  older versions the link's elements between the heights of the view range.
- **Routes are 2D**. Heights are ignored: a duct above the ceiling that the
  plan shows is an obstacle. Untick a category to route under / over it.
- **Rotated plans**: lines are horizontal and vertical on the drawing (the
  view's own directions), not along project north.
- **A panel on a wall** sits inside the wall's clearance. Within 600 mm of the
  panel and of the earth bar the line may leave that zone. If it has to cross
  an obstacle there, the row is yellow and says CHECK.
- **Walls**: with Walls ticked, rooms are closed. "Pass through door openings"
  leaves each door open (door width minus the clearance on both sides). A
  panel in a room with no door gives FAILED - no wrong line is drawn.
- **Already connected**: the lines carry the panel and earth bar they belong
  to. Creating the same connection again asks Skip / Replace Existing / Cancel.
- **Connection point**: electrical connector, else the location point, else
  the centre of the bounding box.

## Settings (`er_config.py`)

| Setting | Default | Meaning |
|---|---|---|
| `BUFFERS_MM` | 2000, 6000, 15000 | how far the search region reaches beyond a panel and the earth bar; the next is tried when no route is found |
| `ESCAPE_MM` | 600 | distance from the two ends within which the line may leave a clearance zone |
| `TURN_PENALTY_MM` | 600 | one bend costs as much as this length of line (higher = fewer bends) |
| `MAX_GRID_CELLS` | 1,500,000 | above this the grid is coarsened and the row says so |
| `OBSTACLES` | 14 groups | labels, Revit categories and default ticks |
| `PREVIEW_COLOUR`, `PREVIEW_WEIGHT` | magenta, 6 | how a preview line looks |

## Files

| File | Purpose |
|---|---|
| `script.py` | starts the window (kept tiny on purpose) |
| `er_window.py` | the window and the order of the steps |
| `er_revit.py` | picking, obstacles (host + links), detail lines, tagging |
| `er_geometry.py` | grid, A* path search, clean-up of the path - no Revit API |
| `er_config.py` | settings |
| `ui.xaml` | the window layout |

## How the routing works

1. Every position is reduced to (u, v) in the plan of the view.
2. For each panel a grid is laid over the box round the panel and the earth
   bar plus a buffer - never over the whole building.
3. Each obstacle marks its cells: the body, a band of the clearance round it,
   and a thin band beyond that which routes prefer to stay out of.
4. A* searches from the panel to the earth bar with horizontal / vertical
   steps only. A turn costs extra, so the result has few bends.
5. The path is reduced to its corner points, the first and last leg are slid
   onto the exact connection points, and one detail line is drawn per leg.
