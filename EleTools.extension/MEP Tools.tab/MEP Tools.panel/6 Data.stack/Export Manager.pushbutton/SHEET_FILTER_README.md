# Sheet Search & Filter — implementation notes

Adds parameter-based sheet searching to Export Manager. Filter the sheet
list by one or more sheet parameters (AND across rules, OR within a rule),
review the matches in the existing selection grid, then export only what
is ticked.

## Files

| File | Status | Purpose |
| --- | --- | --- |
| `sheet_filter.py` | **new** | All logic: parameter discovery, value reading, caching, matching. No WPF. |
| `sheet_filter_ui.py` | **new** | WPF controller for the filter panel. No Revit querying. |
| `ExportManager.xaml` | changed | New `Sheet Search & Filter` expander on the Selection tab + `Filter Values` grid column. |
| `ui.py` | changed | Two-stage filter pipeline, panel wiring, Select Matching / Deselect All. |
| `helpers.py` | changed | `ExportItem.FilterValues` field. |

The layering mirrors the existing `filename_builder` / `format_manager`
split: logic modules are configured once and driven by the UI module.

## Data flow

```
ui.load_sheets()
  └─ sheet_filter_ui.reload(sheet elements)     # parameter list, cache reset
       └─ sheet_filter.set_sheets(...)

ui.filter_items()                               # runs on every change
  ├─ Stage 1  sheet_filter_ui.apply_to(items)
  │             └─ sheet_filter.filter_items()  # AND across enabled rules
  │             └─ sheet_filter.describe()      # fills item.FilterValues
  └─ Stage 2  free-text Search box
  └─ _set_grid_items(...)                       # existing grid + count
```

Export is unchanged: `btnExport` still exports the ticked rows, so
"Export Selected Sheets Only" is satisfied by ticking the filtered rows
and leaving Export Range on *Selected items*.

## Operators

| Operator | Input | Notes |
| --- | --- | --- |
| `is any of` / `is none of` | multi-select value list | Values inside one rule are OR-ed |
| `contains` / `does not contain` / `starts with` / `ends with` | text box | Case-insensitive |
| `is empty` / `is not empty` | — | Matches missing *and* blank parameters |

`<none>` in the value list represents sheets where the parameter is blank,
absent, or unreadable — pick it to target exactly those sheets.

Adding a rule for a parameter + operator pair that already exists replaces
its values instead of stacking two contradictory AND conditions.

## Behaviour worth knowing

- **Missing parameters never raise.** `read_value()` returns `""` for a
  parameter that is absent, empty, or throws on read, so one bad sheet
  cannot break a filter pass.
- **Views mode.** The panel is greyed out (sheet parameters do not apply)
  and stage 1 is skipped.
- **Ticks persist across filter changes.** This matches the existing
  Selection Set behaviour, so the count label now warns when checked
  sheets are hidden: `Selected : 12   (3 not shown by the current filter)`.
  `Deselect All` clears hidden rows too; `Select Matching` only ticks
  what is on screen.
- **Rules survive a reload** unless their parameter no longer exists in
  the model, in which case they are dropped silently.
- **Backwards compatible.** `sheet_filter_ui.is_available()` returns
  `False` if the XAML has not been updated, and every new control is
  looked up through the same optional-control pattern `ui.py` already
  uses — so an older `ExportManager.xaml` still runs.

## Performance

Parameter reads are the expensive part, so each parameter is read at most
once per session into an index of `{parameter: {sheet id: value}}`:

- **Discovery** samples the first 25 sheets only
  (`PARAMETER_SAMPLE_SIZE`) — sheet parameters are category-wide.
- **First** filter on a parameter costs one pass over the sheets.
- **Every later** evaluation is a dict lookup. Offline benchmark with
  3,000 sheets and 3 rules: 11 ms cold, 2.7 ms warm.
- `sheet_filter.invalidate()` drops the cache; `set_sheets()` calls it,
  so re-clicking *Sheets* picks up parameter edits made in Revit.

Choosing a parameter in the drop-down triggers its first read pass in
order to list distinct values. On very large models expect a brief pause
there, not while filtering.

## Extending

- **Persist presets** — `FilterCriterion` is JSON-shaped
  (`Parameter` / `Operator` / `Values` / `IsEnabled`); reuse
  `selection_sets.py` for save/load.
- **OR groups** — `sheet_filter.matches()` is the single place the AND is
  applied.
- **More operators** — add the constant, list it in `OPERATORS`, add the
  branch in `_matches_criterion()`, and register it in
  `VALUE_LIST_OPERATORS` / `TEXT_OPERATORS` / `NO_VALUE_OPERATORS`.
- **Project Information parameters** — `filename_builder` already exposes
  these under a `Project: ` prefix; the same trick works here if you want
  to filter by them (they are constant per document, so it is only useful
  for linked-model workflows).
