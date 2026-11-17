# Circuit Breaker Tools — pyRevit Extension

Adds a **CB Tools** tab to Revit with four buttons on the **Circuit
Breakers** panel:

- **Auto Number** — number a selection of breakers sequentially (CB-01,
  CB-02, ...) into a parameter.
- **Transfer Parameter** — copy a parameter value between two selections,
  matched one-to-one (nearest-position / ordered / manual).
- **Manual Transfer** — dedicated pair-at-a-time version of manual
  matching, for far-apart or irregularly-routed one-to-one connections.
- **Feed From Panel** — **one-to-many** broadcast: pick a single source
  panel/board and write its value into every connected cable, breaker,
  isolator, or sub-panel it feeds, all at once.

Both tools share the same tag-filtering and sorting logic (in
`lib/cb_common.py`) so tags/text/dimensions never get swept into either
one, and both support sorting picks by position in the view instead of
click order.

---

## 1. Auto Number

Lets you select circuit breaker detail items / family instances on a
single-line diagram (like the ones in your `STADIUM ELECTRICAL SERVICES`
views) and write sequential values such as `CB-01`, `CB-02`, `CB-03` ...
into a chosen instance parameter (e.g. `MEE-CBRef`).

### What it does

1. Uses your **current Revit selection** if you already selected elements,
   otherwise it prompts you to click elements one by one (in the order you
   want them numbered).
2. Opens a window where you set:
   - **Target Parameter** — defaults to `MEE-CBRef`, but the box is
     editable/searchable and is pre-populated with writable text
     parameters found on the selected elements (so it also works for
     `MEE-EquipmentTag`, `Mark`, `Comments`, etc.).
   - **Prefix** — e.g. `CB-`
   - **Start Number**, **Increment**, **Padding** (digits, e.g. `2` → `01`)
   - **Sort Order** — selection order as picked, or automatically sort the
     selected elements left‑to‑right / right‑to‑left / top‑to‑bottom /
     bottom‑to‑top based on their position in the active view. This is
     useful when you box-select a whole row of breakers (like CB‑01…CB‑08
     in your panel schedule) and just want them numbered left to right
     without clicking them one at a time.
3. Shows a **live preview table** (Element Id, Category, Family/Type,
   Current Value, New Value) so you can check everything before writing
   anything to the model.
4. **Apply Numbering** writes the values inside a single transaction.
   Elements where the target parameter doesn't exist, is read‑only, or
   isn't a text parameter are skipped and reported at the end — nothing
   partially-written is hidden from you.

---

## 2. Transfer Parameter

Copies a value from a **source** family's parameter into a **target**
family's parameter — for example, taking an Isolator's `MEE-Component`
value and writing it into a downstream Cable's `MEE-Feeder` parameter, as
in your example screenshots.

### What it does

1. **Step 1 — pick SOURCE elements.** Uses your current Revit selection
   if you already selected something, otherwise prompts you to click
   elements. Source elements can be **mixed families** — e.g. Isolators,
   MCCB blocks, schedule-header blocks — the tool doesn't care what
   family an element is, only that it has one of the source parameters.
2. **Step 2 — pick TARGET elements.** Prompts you to click the elements
   to write into (e.g. the Cables).
3. Opens a window with three ways to match source ↔ target:
   - **Nearest position (default)** — greedy matching weighted heavily
     toward X-alignment ("same column"), so a source far above its real
     target (across level break-lines) still matches correctly instead of
     losing to a vertically-closer decoy. This assumes the connection
     runs in a straight column, though — it does **not** trace routed
     connector lines, so it can't guess a connection that snakes
     sideways into a different column (e.g. a panel drawn off to one
     side and fed via a bent/routed line).
   - **Ordered** — sort each side independently (selection order or
     position) and pair by index. Only reliable when both sides are
     perfectly aligned 1:1.
   - **Manual** — click one source element, then its matching target,
     one pair at a time, using the **+ Add Pair** button (which reopens
     the picker, then reopens this window with the pair added). This is
     the only option that's always correct regardless of distance or how
     a connection is routed — use it for irregular layouts, like a
     cluster of panels drawn off to the side and fed via bent lines
     rather than a straight drop. **Undo Last Pair** removes the most
     recent one if you misclick.
4. **Source Parameter field accepts a comma-separated list**, e.g.
   `MEE-Component, MEE-EquipmentTag, Mark` — each source element tries
   them in order and uses the first one that actually has a value. This
   fixes silent blank copies that happened when a different family stored
   the tag under a different parameter name.
5. **Overwrite existing target values** checkbox — leave it checked to
   always overwrite, or uncheck it to only fill in targets that are
   currently blank.
6. A **live preview table** shows every pair — source id/type, which
   parameter actually matched, source value, target id/type/current
   value/new value — so nothing is a surprise. Rows where no source
   value was found are clearly marked `<not found>` / `<none found>`
   instead of silently writing a blank, and unmatched or unpositioned
   elements are called out in the status line.
7. **Apply Transfer** writes the values in a single transaction, skips
   rows with no source value or an invalid target parameter, and reports
   exactly what happened (with Element Ids) afterward.
8. **Pick Source Elements** / **Pick Target Elements** buttons inside the
   window let you redo either side without restarting the tool.

### Typical workflow for your example

- Click **Transfer Parameter**.
- Step 1: select all the source elements across every family that carries
  the tag you want to copy (Isolators, MCCB blocks, schedule headers,
  etc. — mix them freely).
- Step 2: select the corresponding Cables.
- In the window: Source Parameter = `MEE-Component, MEE-EquipmentTag`,
  Target Parameter = `MEE-Feeder`, Pairing Method = `Nearest position`
  (the default).
- Click **Update Preview**. Check the **Matched Param** column so you can
  see which of your comma-separated source names actually hit for each
  row, and confirm each New Target Value looks right.
- Click **Apply Transfer**.

---

## 3. Manual Transfer

A leaner, dedicated version of Transfer Parameter's manual pairing mode -
for when you already know the layout is irregular (elements far apart,
or connected via bent/routed lines instead of a straight column) and
want to skip straight to pairing without a bulk-selection step first.

### What it does

1. Opens directly into an empty pairing list - no upfront source/target
   selection required.
2. **+ Add Pair** — prompts you to click one source element, then its
   matching target element, and adds that pair to the preview table.
   Repeat for each connection.
3. **Undo Last Pair** removes the most recent pair; **Clear All** starts
   over.
4. Same **Source Parameter(s)** comma-separated fallback list and
   **Target Parameter** field as the main tool (defaults to
   `MEE-Component, MEE-EquipmentTag` → `MEE-Feeder`).
5. Live preview shows each pair with the matched source parameter and the
   value that will be written, same as the other tools.
6. **Apply Transfer** writes everything in one transaction and reports
   anything skipped.

### Typical workflow

- Click **Manual Transfer**.
- Click **+ Add Pair**, click the source box, then click its target
  cable. Repeat for every connection you need, however far apart or
  oddly routed they are.
- Confirm the preview, then **Apply Transfer**.

---

## 4. Feed From Panel (one-to-many broadcast)

For the very common case in your panel schedules: one panel/board (e.g.
`USMDB-ST-SW-B1-03-01L`) feeds a whole row of downstream cables,
breakers, isolators, and sub-panels — and every single one of them needs
the same `MEE-FedFrom` value, not a unique paired value. This is
different from Transfer Parameter, which matches one source to one
target each; this tool takes **one source and applies it to as many
targets as you like in a single pass**.

### What it does

1. **Pick Source Panel** — click the single panel/board element (e.g. the
   `S_EC_Panel` detail item showing `MEE-Component`).
2. **Targets** — if you already had elements selected before running the
   tool (e.g. you box-selected the whole row of 8 cables/breakers, like
   `Detail Items (8)` in your screenshot), that selection is used
   automatically. Otherwise you're prompted to pick them.
3. Set **Source Parameter(s)** (comma-separated fallback list, defaults to
   `MEE-Component, MEE-EquipmentTag`) and **Target Parameter** (defaults
   to `MEE-FedFrom`).
4. **Overwrite existing target values** checkbox, same as the other
   tools.
5. Live preview shows every target with its current value and the value
   it will receive (all the same value, from the one source).
6. **Apply to All Targets** writes it everywhere in a single transaction.
7. **Pick Source Panel** / **Pick Target Elements** buttons inside the
   window let you redo either side without restarting.

### Typical workflow for your example

- Box-select all 8 cable/breaker detail items fed by the panel (or select
  them however you like).
- Click **Feed From Panel** — it uses your selection as the targets
  automatically, then prompts you to click the source panel.
- Click the panel once.
- Confirm the preview shows `MEE-FedFrom` = the panel's `MEE-Component`
  value (`USMDB-ST-SW-B1-03-01L`) for every row, then **Apply to All
  Targets**.

---

## Installation

1. Copy the whole `CircuitBreakerTools.extension` folder to your pyRevit
   extensions folder, e.g.:
   `%APPDATA%\pyRevit\Extensions\CircuitBreakerTools.extension`
   (or any folder you've registered with pyRevit via
   `pyRevit > Settings > Custom Extension Directories`).
2. In Revit, go to the **pyRevit** ribbon tab → **Reload** (or restart
   Revit).
3. A new **CB Tools** tab will appear with a **Circuit Breakers** panel
   containing the **Auto Number**, **Transfer Parameter**, **Manual
   Transfer**, and **Feed From Panel** buttons.

## Typical workflow for Auto Number (panel schedule)

- Open the single-line diagram view (e.g. `STADIUM ELECTRICAL SERVICES`).
- Either:
  - Pre-select CB‑01 through CB‑08 in Revit (ctrl‑click, or a selection
    box), **then** click **Auto Number** — it will use your selection as-is,
    or
  - Click **Auto Number** first with nothing selected, then click each
    breaker in order when prompted.
- In the window: set **Target Parameter** to `MEE-CBRef`, **Prefix** to
  `CB-`, **Start Number** to `1`, **Padding** to `2`, and **Sort Order** to
  `Left to right (X position)` if you box-selected instead of clicking in
  order.
- Click **Update Preview**, confirm the New Value column reads
  `CB-01, CB-02, CB-03 ...` in the order you expect, then **Apply
  Numbering**.

## Notes / customization

- The parameter lists, prefixes, numbering scheme, and sort logic are all
  editable directly in each `script.py` / `ui.xaml` if you want to bake in
  your firm's specific defaults (e.g. always default Transfer Parameter to
  `MEE-Component` → `MEE-Feeder`, or add a per-panel numbering prefix like
  `DB1-CB-`).
- The sort logic uses each element's bounding box midpoint in the active
  view, so it works for both true family instances and 2D detail items
  drawn on single-line diagrams.
- Shared logic (tag filtering, sorting, parameter helpers) lives in
  `lib/cb_common.py`, which pyRevit automatically adds to `sys.path` for
  every script in this extension — edit it once and both tools pick up
  the change.
- Written for pyRevit's IronPython/CPython engine; no external
  dependencies beyond pyRevit + the Revit API.
