# Transfer Load Name — code review and rewrite

Reviewed against Revit API 2021–2026 and pyRevit 4.8 / 5.x (IronPython 2.7 engine).

---

## Correctness bugs

### 1. `bundle.yaml` caption never wrapped
```yaml
title: Transfer\nLoad Name      # plain scalar
```
YAML plain scalars do not process backslash escapes, so the ribbon caption
rendered literally as `Transfer\nLoad Name`. Fixed by double-quoting, which
makes YAML interpret the escape:
```yaml
title: "Transfer\nLoad Name"
```

### 2. Wrong parameter in the fallback path
```python
return circuit.LookupParameter('Circuit Load Name')
```
These are two different parameters:

| BuiltInParameter | User-visible name | Lives on | Writable |
|---|---|---|---|
| `RBS_ELEC_CIRCUIT_NAME` | **Load Name** | ElectricalSystem | yes |
| `RBS_WIRE_CIRCUIT_LOAD_NAME` | **Circuit Load Name** | wires / connected elements | no (reporting) |

The code targets the first but names the second in the fallback, the tooltip,
and the output headings. Anything that did reach that fallback would resolve a
read-only reporting parameter, and `Set()` would fail. Now the correct BIP is
used, with name aliases only as a genuine last resort.

### 3. Dead `try/except` around `get_Parameter`
```python
try:
    return circuit.get_Parameter(...)
except Exception:
    return circuit.LookupParameter(...)
```
`get_Parameter` returns `None` when the parameter is absent — it does not
raise. The fallback branch was unreachable. Now a `None` check drives it.

### 4. Cancelling the progress bar committed partial work
```python
with revit.Transaction('Transfer Circuit Load Names'):
    ...
        if progress.cancelled:
            break
```
`break` exits the loop cleanly, so the context manager commits everything
written so far. "Cancel" silently half-applied the operation. Replaced with an
explicit `DB.Transaction` that calls `RollBack()` on cancel.

### 5. `ElementId.IntegerValue` breaks on Revit 2024+
`IntegerValue` is deprecated in 2024 in favour of the 64-bit `Value`. Used in
`get_value()` and `label()`. Wrapped in a version-safe `eid_value()` helper.

### 6. The preview table never rendered as a table
```python
for item in changes[:50]:
    output.print_md('| {0} | ... |')
```
Each `print_md` call is its own markdown block in the pyRevit output window, so
the rows never coalesce. Replaced with `output.print_table()`.

### 7. `LookupParameter` picks the wrong duplicate
`get_value()` used `item.LookupParameter(name)`, which returns the *first*
match by name. When a family parameter and a shared parameter share a name —
routine in MEP content — it can return the empty one and the circuit gets
skipped. `read_text()` now walks `element.Parameters` and lets a non-empty
match win.

### 8. Unguarded `.Name` and `.CircuitNumber`
`label()` and `output.linkify(item[4].Id, item[4].Name)` call `Element.Name`,
which raises for several element types, from inside the reporting loop — one
bad element aborted the whole report. Both are guarded now.

Also note `ElectricalSystem.Name` *is* the Load Name, so the old label showed
the value the tool was about to overwrite. Labels now use `CircuitNumber` and
`PanelName`.

### 9. No document guard
`revit.doc` was used at import time with no check for a family document or no
open document. Added.

---

## Performance

`get_choices()` walked **every** parameter of **every** connected element of
**every** circuit. On a model with a few thousand circuits that is millions of
COM-boundary reads before the first dialog appears.

An element's parameter set is determined by its type and category, so
`scan_parameter_names()` now inspects one representative per
`(type id, category id)` pair. Also switched the model-wide collector from
`OfClass(ElectricalSystem)` to
`OfCategory(OST_ElectricalCircuit).WhereElementIsNotElementType()`.

---

## UI rewrite

The original flow was five sequential modal dialogs with no way back: alert →
`CommandSwitchWindow` → `SelectFromList` → alert → alert. The preview landed in
the output window *after* the parameter was already locked in, so a wrong pick
meant restarting.

`ui.xaml` replaces that with one dialog:

- **Scope** — current selection / active view / entire model, switchable live.
  Selecting equipment or devices now resolves the circuits they belong to via
  `MEPModel.GetElectricalSystems()`, rather than requiring circuits to be
  selected directly.
- **Source** — parameter on the circuit, or on a connected element.
- **Parameter** — filterable combo box, repopulated when scope or source
  changes, preserving the current pick where possible.
- **Options** — overwrite existing values, trim whitespace, prefix/suffix.
- **Live preview grid** — circuit, panel, current value, new value, source
  element, and a per-row action (`Update`, `Already set`, `No change`,
  `Source empty`, `Read-only`, `No Load Name param`). Double-click a row to
  select that circuit in Revit.
- **Apply** is disabled until at least one row will actually change.

The transaction runs *after* the window closes, so no modal dialog is ever open
inside a transaction.

---

## Notes for deployment

- Targets the **IronPython 2.7** engine (pyRevit's default). WPF data binding
  to plain Python objects is less reliable under the CPython/pythonnet engine —
  do not add a `#! python3` shebang without retesting the grid.
- `ui.xaml` deliberately has no `x:Class` attribute. pyRevit loads it with
  `wpf.LoadComponent` (IronPython's DynamicXamlReader), which binds `x:Name`
  fields and event handlers onto the Python window instance; a declared
  code-behind class conflicts with that.
- Logic layer is covered by `harness_test.py` (23 assertions against stubbed
  Revit objects: facet collection, category/type filtering, parameter scan,
  preview states, prefix/suffix, row search). Untested against a live Revit
  session — there is no Revit in this
  environment. Syntax, XAML well-formedness, and handler/control name pairing
  are verified; behaviour against real circuits is not.
- Worth a look if you take this further: `ElectricalSystem.LoadName` is a
  read/write property and would bypass parameter resolution entirely. The
  parameter route is kept here so `IsReadOnly` and workset-ownership failures
  surface uniformly.

---

## Filter and search (second pass)

Previously every element on a circuit was a candidate source, so on a circuit
carrying receptacles, lighting and a disconnect, whichever had the lowest
element id won. Added:

**Category filter.** Checkbox list of every category present among the
connected elements, with live counts — `Electrical Fixtures (412)`,
`Lighting Fixtures (188)`, and so on. Built by one pass over
`circuit.Elements` with no parameter reads. All / None buttons.

**Family : Type filter.** Second checkbox list, narrowed to the ticked
categories and searchable by its own text box. Labels resolve via
`FamilySymbol.FamilyName`, falling back to `ALL_MODEL_FAMILY_NAME`. All /
None act on the *listed* types, not the whole model, so you can search
`recept`, hit All, and tick just those.

**Preview search.** Space-separated terms, AND-matched across circuit number,
panel, current value, new value, source element, category and action. Plus a
"Changes only" toggle.

Both facet lists feed `SourceFilter`, which is also what `scan_parameter_names`
consumes — so narrowing to Electrical Fixtures shrinks the parameter dropdown
to what those fixtures actually carry, rather than the union across every
connected category.

Three implementation notes worth knowing if you edit this:

- Search is **not** cosmetic. Apply acts on the rows left visible, so filtering
  to one panel and hitting Apply touches only that panel. The status line and
  the button label (`Apply (34)`) state the count rather than leaving it to be
  inferred.
- A facet list with everything ticked passes `None` rather than a set of all
  keys, so the default path costs no per-element membership tests.
- Facets are collected lazily. In circuit-parameter mode they are never
  consulted, so walking every connected element model-wide to build them would
  be pure waste; switching to element mode collects them then.
- `FacetItem` has no `INotifyPropertyChanged` — implementing .NET events from
  IronPython is fiddly and nothing needs source-to-UI push. The All / None
  buttons rebind `ItemsSource` to repaint instead. Related: `on_type_changed`
  reads only the *listed* items, because items hidden by the search box or the
  category filter hold stale `IsSelected` values that would otherwise be
  written back over the saved state.

---

## Fix: XAML load crash on attached events

Reported traceback:

```
Exception: Set property 'Microsoft.Internal.Scripting.Runtime
           .DynamicXamlReader+DynamicWriter.Click' threw an exception
---> System.Reflection.TargetException: Object does not match target type
   at System.Reflection.EventInfo.AddEventHandler(Object target, Delegate handler)
```

Cause: the two facet lists used XAML **attached event** syntax,
`CheckBox.Click="on_category_changed"` on the parent `ListBox`. IronPython's
`DynamicXamlReader` has no attached-event support. It resolved `Click` as an
ordinary CLR event and called `EventInfo.AddEventHandler` with the `ListBox`
as target, but `Click` is declared on `ButtonBase`, so reflection rejected the
target type.

Note the distinction from attached **properties**: `Grid.Row`, `Grid.Column`
and `ScrollViewer.HorizontalScrollBarVisibility` all load fine, and the reader
processed dozens of them before reaching the event. Only attached events fail.

The handler also cannot simply move onto the `CheckBox` inside the
`DataTemplate`. Template contents are instantiated later by WPF's own reader,
which has no reference to the Python window, so the name would not resolve.

Fix: `TransferLoadNameWindow._wire_facet_events` calls

```python
listbox.AddHandler(ButtonBase.ClickEvent, RoutedEventHandler(handler))
```

on both lists after the XAML loads. `AddHandler` takes the `RoutedEvent`
explicitly, so no declaring-type match is required, and the click is caught as
it bubbles out of the template. Behaviour is unchanged.

**Rule of thumb for this codebase:** an event may only appear in the XAML if it
is declared on the element's own type or a base it genuinely inherits.
`MouseDoubleClick` on the `DataGrid` is fine because `DataGrid` is a `Control`.
Anything of the form `Owner.Event="..."` on a different element must be wired
with `AddHandler` in Python.

---

## Third pass: seed selection, filter by value, restyled window (2026-10-05)

### Bugs fixed
1. **Selecting a panel only found the circuit that supplies it.** `circuits_of`
   returned the result of the first API call that gave anything
   (`GetElectricalSystems`), so `GetAssignedElectricalSystems` - the circuits the
   panel FEEDS - was never reached. `systems_of` now combines both calls and
   classifies each circuit by its `BaseEquipment`; the window lets the user
   choose supply / fed / both.
2. **Family : Type list read "Family : Id 123456".** `element.Name` cannot be
   read from IronPython for most element types. `safe_name` now falls back to
   `DB.Element.Name.GetValue()` and the type-name parameter.
3. **The window stalled while typing a prefix / suffix.** Every keystroke
   re-walked every parameter of every candidate element. `ValueReader` and
   `CircuitElements` cache values and connected elements until Refresh, and
   `Element.GetParameters(name)` replaces the walk over `element.Parameters`.
4. **The chosen source parameter was silently replaced** by the first one in the
   alphabet whenever the circuit set was empty for a moment (e.g. while typing
   a filter value). The user's pick is remembered (`_wanted_param`).
5. **Double-click popped up Revit's "no good view" dialog** (`ShowElements` on a
   circuit, which has no graphics). It now selects the circuit and the element
   the text was read from.
6. **A rolled-back commit was reported as success** - `Commit()` returns the
   status instead of raising. `apply_rows` checks it.
7. `LOAD_NAME_ALIASES` still listed "Circuit Load Name", the read-only
   reporting parameter this file warns about. Removed.
8. Ticks in the category / type lists were all reset whenever the circuit set
   changed. They are kept now.

### New
- **Find circuits through**: tick boxes for Circuits, Electrical fixtures,
  Electrical equipment and Other devices, for all three scopes (selection,
  active view, whole model), with a note saying how many circuits came through
  each.
- **Filter by parameter value**: on the circuit or on a connected element;
  any parameter (numbers are compared by their display text, type parameters
  work too); equals / does not equal / contains / does not contain / starts
  with / ends with / is empty / is not empty; the values found in the model can
  be picked from a list. With "on a connected element" only the matching
  elements may supply the text.
- **Type parameters as the source** (Description, Type Mark ...): the instance
  value is used first, then the type's.
- Restyled window (same look as Export Manager / Panel MCB Check), action
  chips in the preview, all handlers wired in `_wire_events` instead of XAML.
- The output window only opens when something failed.

Verified outside Revit: 66 checks against stubbed Revit objects driving the
real script and XAML in pyRevit's IronPython 2.7 engine. Not yet run in Revit.