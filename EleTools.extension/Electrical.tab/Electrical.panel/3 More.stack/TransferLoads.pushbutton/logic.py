# -*- coding: utf-8 -*-
"""Core collection / matching / transfer logic for the Load Transfer tool.

Kept free of any WPF/UI code so it can be tested and reused independently
of ui.py.
"""

import fnmatch

from Autodesk.Revit.DB import (
    BuiltInCategory,
    FilteredElementCollector,
    RevitLinkInstance,
    StorageType,
    Transaction,
    TransactionStatus,
    UnitUtils,
)

try:
    STRING_TYPES = (str, unicode)          # IronPython 2.7 under pyRevit
except NameError:
    STRING_TYPES = (str,)                  # CPython 3 / tests


# ---------------------------------------------------------------------------
# Value normalisation
# ---------------------------------------------------------------------------
# A Text-storage target parameter is the reason this section exists. Its
# value comes back from Revit as a STRING ("30.00"), while the source
# Double comes back as a FLOAT (30.0). Comparing or formatting those two
# by type produces an endless false "replaced" on every run. Everything
# below funnels both sides through one representation.

VALUE_DECIMALS = 2


def coerce_number(value):
    """Return value as a float if it is, or textually looks like, a number.

    Returns None for anything non-numeric, so callers can fall back to a
    plain string comparison. bool is excluded deliberately -- Yes/No
    parameters are not quantities.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, STRING_TYPES):
        text = value.strip().replace(",", "")
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def format_number(value, decimals=VALUE_DECIMALS):
    """The one canonical display/storage form for a numeric value.

    Fixed decimals so '30' and '30.00' can never appear side by side for
    the same quantity. Sub-threshold magnitudes get extra precision
    instead of collapsing to '0.00', which would render a real
    post-multiplier result (0.0004 kW from a 0.4 W source) as an
    apparently empty value in the very column being checked.
    """
    number = coerce_number(value)
    if number is None:
        return None
    if number == 0:
        return "{:.{d}f}".format(0.0, d=decimals)
    if abs(number) < 10 ** -decimals:
        return "{:.4g}".format(number)
    return "{:.{d}f}".format(number, d=decimals)


def as_written_value(param, value):
    """Exactly what set_param_value() will put into `param`.

    The preview compares against THIS, not against the raw float, so the
    dialog can never claim a change that the write would not actually
    make (or miss one that it would).
    """
    if param is None or value is None:
        return value
    if param.StorageType == StorageType.String:
        text = format_number(value)
        return text if text is not None else str(value)
    if param.StorageType == StorageType.Integer:
        number = coerce_number(value)
        return value if number is None else int(round(number))
    if param.StorageType == StorageType.Double:
        number = coerce_number(value)
        return value if number is None else number
    return value


# ---------------------------------------------------------------------------
# Parameter read/write
# ---------------------------------------------------------------------------

def get_param_value(element, param_name):
    """Read a parameter's value, returning None if missing/empty.

    IMPORTANT: Parameter.AsDouble() always returns the value in Revit's
    INTERNAL unit system (feet-based), not whatever's shown in the UI --
    those only coincide when the display unit happens to equal the
    internal unit. For any parameter whose display unit differs (a very
    common real-world case), a naive AsDouble() silently returns a value
    scaled by that conversion factor, producing numbers that look
    plausible but are wrong by a fixed multiplier. We always convert
    through UnitUtils to the parameter's own display unit, so what this
    function returns always matches what a user sees in the Properties
    panel.
    """
    if element is None or not param_name:
        return None
    p = element.LookupParameter(param_name)
    if not p or not p.HasValue:
        return None
    st = p.StorageType
    if st == StorageType.String:
        return p.AsString()
    elif st == StorageType.Double:
        raw = p.AsDouble()
        try:
            unit_type_id = p.GetUnitTypeId()
            if unit_type_id is not None:
                return UnitUtils.ConvertFromInternalUnits(raw, unit_type_id)
        except Exception:
            pass
        return raw
    elif st == StorageType.Integer:
        return p.AsInteger()
    elif st == StorageType.ElementId:
        return p.AsValueString()
    return None


def set_param_value(element, param_name, value):
    """Write a value into a parameter, converting type/units as needed.

    Mirrors get_param_value(): a numeric value is treated as being in the
    parameter's DISPLAY unit and converted to Revit's internal unit before
    Set() is called, so writing 30.0 into a kW-displayed field actually
    stores the correct internal value rather than a raw, unconverted 30.0.

    Returns (success, error_message_or_None).
    """
    p = element.LookupParameter(param_name)
    if not p:
        return False, "parameter not found"
    if p.IsReadOnly:
        return False, "parameter is read-only"
    try:
        if p.StorageType == StorageType.String:
            # Route every numeric through format_number -- including ints,
            # which used to land here as bare "30" and then never compare
            # equal to the "30.00" a float write produced for the same
            # quantity.
            text = format_number(value)
            if text is None:
                text = str(value) if value is not None else ""
            p.Set(text)
        elif p.StorageType == StorageType.Double:
            numeric = float(value)
            try:
                unit_type_id = p.GetUnitTypeId()
                if unit_type_id is not None:
                    numeric = UnitUtils.ConvertToInternalUnits(numeric, unit_type_id)
            except Exception:
                pass
            p.Set(numeric)
        elif p.StorageType == StorageType.Integer:
            p.Set(int(value))
        else:
            return False, "unsupported storage type"
        return True, None
    except Exception as error:
        return False, str(error)


def id_number(element_or_id):
    """The integer of an element id. ElementId.IntegerValue is deprecated from Revit 2024 and
    gone in later versions (.Value replaces it); everything here goes through this helper."""
    element_id = getattr(element_or_id, "Id", element_or_id)
    try:
        return int(str(element_id.Value).rstrip("L"))
    except Exception:
        try:
            return element_id.IntegerValue
        except Exception:
            return -1


def model_name(element):
    """The linked model an element comes from ('' for this model)."""
    try:
        document = element.Document
        if document.IsLinked:
            return u"{}".format(document.Title)
    except Exception:
        pass
    return u""


def element_label(element):
    """Short display label for an element, used in the UI grids. Elements of a linked model carry
    the model name: '[MEP.rvt]' - their Id belongs to that model, not to this one."""
    if element is None:
        return "(none)"
    linked = model_name(element)
    suffix = u"  [{}]".format(linked) if linked else u""
    try:
        name = element.Name
        if name:
            return u"{} (Id {}){}".format(name, id_number(element), suffix)
    except Exception:
        pass
    return u"Id {}{}".format(id_number(element), suffix)


def parameter_scope(instance, param_name):
    """Determine whether a parameter found via instance.LookupParameter is
    actually stored on the instance itself, or on its Type/Symbol -- and
    therefore SHARED across every placed instance of that type.

    This matters because Revit lets you retrieve and even Set() a
    type-bound parameter through a specific instance's LookupParameter,
    but doing so silently changes the value for every other instance of
    that same type too -- a common source of "wrong value" bugs that look
    like a matching problem but are actually a family design issue.

    Returns "instance", "type", "not_found", or "unknown".
    """
    try:
        param = instance.LookupParameter(param_name)
    except Exception:
        return "unknown"
    if param is None:
        return "not_found"
    try:
        owner_id = param.Element.Id
    except Exception:
        return "unknown"
    try:
        if owner_id == instance.Id:
            return "instance"
        symbol = instance.Symbol
        if owner_id == symbol.Id:
            return "type"
    except Exception:
        pass
    return "unknown"


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------

def type_key(instance):
    """'Family : Type' of a family instance - the same text the target detail item list uses."""
    family_name, type_name = "Unknown Family", "Unknown Type"
    try:
        symbol = instance.Symbol
        family_name = symbol.Family.Name
        type_name = symbol.Name
    except Exception:
        pass
    return "{} : {}".format(family_name, type_name)


HOST_MODEL_LABEL = u"This model"


def source_models(doc):
    """[(label, document)] - this model first, then every LOADED linked model. A link placed several
    times is listed once (its elements would otherwise be read twice and every match would turn
    AMBIGUOUS)."""
    out = [(HOST_MODEL_LABEL, doc)]
    seen = set()
    for link in FilteredElementCollector(doc).OfClass(RevitLinkInstance):
        try:
            link_doc = link.GetLinkDocument()
        except Exception:
            link_doc = None
        if link_doc is None:
            continue                                  # unloaded link: nothing to read
        key = (link_doc.PathName or link_doc.Title).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append((u"{}".format(link_doc.Title), link_doc))
    return out


def _models(doc, models):
    """The documents to read: the given list, or just this model."""
    return list(models) if models else [doc]


def collect_fixture_type_groups(doc, models=None):
    """Placed Electrical Fixtures of the source models grouped by 'Family : Type' -> {key: [instances]}
    (for the fixture filter). The same type in two models is one entry."""
    groups = {}
    for document in _models(doc, models):
        for instance in (FilteredElementCollector(document)
                         .OfCategory(BuiltInCategory.OST_ElectricalFixtures)
                         .WhereElementIsNotElementType()
                         .ToElements()):
            groups.setdefault(type_key(instance), []).append(instance)
    return groups


def collect_source_elements(doc, include_equipment=True, include_fixtures=True, fixture_types=None,
                            models=None):
    """Collect Electrical Equipment and/or Electrical Fixtures instances from the source models.

    models: the documents to read (this model and / or linked models - see source_models); None = this
    model only. Linked elements are only READ (their values go into this model's detail items).
    fixture_types: None = every Electrical Fixture; otherwise a set of 'Family : Type' keys and only
    fixtures of those types are used (the Electrical Fixture filter in the window). Equipment is
    never filtered by it."""
    elements = []
    for document in _models(doc, models):
        if include_equipment:
            elements.extend(
                FilteredElementCollector(document)
                .OfCategory(BuiltInCategory.OST_ElectricalEquipment)
                .WhereElementIsNotElementType()
                .ToElements()
            )
        if include_fixtures:
            fixtures = (FilteredElementCollector(document)
                        .OfCategory(BuiltInCategory.OST_ElectricalFixtures)
                        .WhereElementIsNotElementType()
                        .ToElements())
            if fixture_types is None:
                elements.extend(fixtures)
            else:
                elements.extend(f for f in fixtures if type_key(f) in fixture_types)
    return elements


def collect_detail_item_groups(doc):
    """Group all placed Detail Item instances by 'Family : Type'.

    Returns a dict: {"Family : Type": [instances]}.
    """
    instances = (
        FilteredElementCollector(doc)
        .OfCategory(BuiltInCategory.OST_DetailComponents)
        .WhereElementIsNotElementType()
        .ToElements()
    )

    groups = {}
    for instance in instances:
        groups.setdefault(type_key(instance), []).append(instance)
    return groups


def common_parameter_names(elements, sample_size=8):
    """Parameter names shared by a sample of elements (for dropdowns)."""
    common = None
    for element in elements[:sample_size]:
        current = set()
        try:
            for parameter in element.Parameters:
                try:
                    definition = parameter.Definition
                    if definition and definition.Name:
                        current.add(definition.Name)
                except Exception:
                    pass
        except Exception:
            pass
        common = current if common is None else (common & current)
    return sorted(common or [], key=lambda value: value.lower())


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def category_name(element):
    """Human-readable Revit category name, for diagnosing ambiguous matches."""
    try:
        return element.Category.Name
    except Exception:
        return "Unknown category"


def match_key(value):
    """Comparison form of a match value: 'DB-01 ', 'db-01' and 'DB-01' are the same panel.
    Numbers compare by value (30 and '30.00' match). Empty -> None."""
    if value is None:
        return None
    number = coerce_number(value)
    if number is not None and not isinstance(value, STRING_TYPES):
        return format_number(number)
    text = " ".join(str(value).split()).upper()
    return text or None


def build_matches(source_elements, target_elements, match_source_param, match_target_param):
    """Pair each target (detail item) with a source sharing the same
    match-parameter value.

    IMPORTANT: if more than one source element shares the same match value,
    that pairing is AMBIGUOUS and is deliberately NOT resolved automatically
    -- picking one silently would risk writing the wrong element's data
    (e.g. a different panel's load value) into the target. Ambiguous
    targets are flagged so the user can either fix the underlying data
    (make the match value unique) or choose a more specific matching
    parameter, instead of getting a wrong value with no warning.

    Returns a list of dicts:
        {source, target, key, matched, status, ambiguous_count,
         ambiguous_breakdown}
    status is one of: "matched", "ambiguous", "unmatched", "no_key"
    ambiguous_breakdown is a string like "1 Electrical Equipment, 1
    Electrical Fixtures" so you can tell at a glance whether the conflict
    is across categories (e.g. a panel AND its isolator sharing one tag)
    or duplicates within the same category.
    """
    source_by_key = {}
    for element in source_elements:
        key = match_key(get_param_value(element, match_source_param))
        if not key:
            continue
        source_by_key.setdefault(key, []).append(element)

    matches = []
    for target in target_elements:
        key = get_param_value(target, match_target_param)

        if not match_key(key):
            matches.append({
                "source": None, "target": target, "key": key,
                "matched": False, "status": "no_key", "ambiguous_count": 0,
                "ambiguous_breakdown": ""
            })
            continue

        sources = source_by_key.get(match_key(key), [])
        ambiguous_breakdown = ""
        if len(sources) == 0:
            status, source, ambiguous_count = "unmatched", None, 0
        elif len(sources) == 1:
            status, source, ambiguous_count = "matched", sources[0], 0
        else:
            # Multiple source elements share this value -- refuse to guess.
            status, source, ambiguous_count = "ambiguous", None, len(sources)
            counts = {}
            for candidate in sources:
                cat = category_name(candidate)
                linked = model_name(candidate)
                if linked:
                    cat = u"{} [{}]".format(cat, linked)   # e.g. the same panel in this model AND a link
                counts[cat] = counts.get(cat, 0) + 1
            ambiguous_breakdown = ", ".join(
                "{} {}".format(count, cat) for cat, count in sorted(counts.items())
            )

        matches.append({
            "source": source,
            "target": target,
            "key": key,
            "matched": status == "matched",
            "status": status,
            "ambiguous_count": ambiguous_count,
            "ambiguous_breakdown": ambiguous_breakdown
        })
    return matches


# ---------------------------------------------------------------------------
# Filtering (which matched rows are actually allowed to transfer)
# ---------------------------------------------------------------------------

FILTER_MODES = ("contains", "starts", "ends", "exact")


def parse_filter_keywords(text):
    """Split a raw filter string into individual keywords.

    Accepts commas, semicolons or newlines as separators, so 'AHU, HRU'
    and 'AHU; HRU' both work. Empty fragments are dropped, so a trailing
    comma can't silently turn into a blank keyword that matches
    everything.
    """
    if not text:
        return []
    raw = str(text)
    for separator in (";", "\n", "\r", "\t"):
        raw = raw.replace(separator, ",")
    return [part.strip() for part in raw.split(",") if part.strip()]


def value_matches_keywords(value, keywords, mode="contains"):
    """True if `value` matches ANY of `keywords` (case-insensitive).

    An empty keyword list means "no filter", so everything matches --
    the caller decides whether that's the right default.

    A keyword containing * or ? is treated as a wildcard pattern
    (fnmatch) regardless of `mode`, so 'AHU-0*' behaves the way anyone
    typing that would expect rather than being taken literally.
    """
    if not keywords:
        return True
    text = ("" if value is None else str(value)).strip().lower()
    if not text:
        return False

    for keyword in keywords:
        needle = str(keyword).strip().lower()
        if not needle:
            continue
        if "*" in needle or "?" in needle:
            if fnmatch.fnmatch(text, needle):
                return True
            continue
        if mode == "starts":
            hit = text.startswith(needle)
        elif mode == "ends":
            hit = text.endswith(needle)
        elif mode == "exact":
            hit = text == needle
        else:
            hit = needle in text
        if hit:
            return True
    return False


def keyword_filter_decision(values, keywords, mode="contains", exclude=False):
    """Decide whether a row survives the filter.

    values : list of candidate strings to test (e.g. match value, target
             name, source name) -- a hit on any one of them counts.
    exclude: invert the result, i.e. "transfer everything EXCEPT these".

    With no keywords the row is always included, whether or not
    `exclude` is ticked -- an empty box means the filter is off, never
    "exclude everything".
    """
    if not keywords:
        return True
    hit = any(value_matches_keywords(value, keywords, mode) for value in values)
    return (not hit) if exclude else hit


def preview_transfer(matches, param_map):
    """Dry run: for each matched pair, compute what WOULD be written for
    every mapping, without changing anything in the model.

    param_map: list of (source_param_name, target_param_name, multiplier).

    Returns a list of dicts (same order as matches), each with:
        target, matched, status,
        previews: list of {source_param, target_param, current_value,
                            new_value, will_write, issue}
    issue is None when the write would succeed; otherwise a short reason
    it would be blocked (read-only, not found, TYPE-bound, no source
    value, etc.) -- exactly the same checks the real transfer uses, so
    what you see here is what you'll get if you actually run it.
    """
    results = []
    for match in matches:
        target = match["target"]
        source = match["source"]
        previews = []

        if match["matched"]:
            for src_param, tgt_param, multiplier in param_map:
                if not src_param or not tgt_param:
                    continue

                current_value = get_param_value(target, tgt_param)
                source_value = get_param_value(source, src_param)

                new_value = None
                issue = None
                warning = None

                if source_value is None:
                    issue = "source has no value"
                else:
                    if isinstance(source_value, (int, float)) and multiplier != 1.0:
                        new_value = source_value * multiplier
                    else:
                        new_value = source_value

                    param = target.LookupParameter(tgt_param)
                    if param is None:
                        issue = "target parameter not found"
                    elif param.IsReadOnly:
                        issue = "target parameter is read-only"
                    elif parameter_scope(target, tgt_param) == "type":
                        issue = "target parameter is TYPE-bound (shared across instances)"
                    elif (param.StorageType in (StorageType.Double, StorageType.Integer)
                          and coerce_number(new_value) is None):
                        # set_param_value() would fail on float()/int() -- block it HERE so the
                        # review shows it instead of the transfer reporting an error afterwards.
                        issue = "source value '{}' is not a number (target is numeric)".format(new_value)
                    elif source is not None and parameter_scope(source, src_param) == "type":
                        warning = (
                            "source '{}' is TYPE-bound -- every instance of that "
                            "source's family type shares this exact value, so it "
                            "may be a fixed spec/rating rather than a unique "
                            "per-installation figure".format(src_param)
                        )

                    if issue is None:
                        # Normalise to what set_param_value() will really
                        # store, so the preview cannot disagree with the
                        # write (a Text target stores "30.00", not 30.0).
                        new_value = as_written_value(param, new_value)

                previews.append({
                    "source_param": src_param,
                    "target_param": tgt_param,
                    "current_value": current_value,
                    "new_value": new_value,
                    "will_write": issue is None,
                    "issue": issue,
                    "warning": warning
                })

        results.append({
            "target": target,
            "matched": match["matched"],
            "status": match["status"],
            "previews": previews
        })
    return results


# ---------------------------------------------------------------------------
# Before/after report
# ---------------------------------------------------------------------------

def values_equal(old_value, new_value):
    """True if writing new_value over old_value would change nothing.

    Numbers are compared with a small relative tolerance rather than ==,
    because a value that has already been transferred once will come back
    from Revit's internal unit conversion a few floating-point bits away
    from the number that was written. A strict == would report those
    rows as changing on every single run.
    """
    if old_value is None and new_value is None:
        return True
    if old_value is None or new_value is None:
        return False
    old_number = coerce_number(old_value)
    new_number = coerce_number(new_value)
    if old_number is not None and new_number is not None:
        # Deliberately coerced rather than isinstance-checked: a Text
        # target holds "30.00" while the Double source holds 30.0, and
        # those are the same quantity.
        difference = abs(old_number - new_number)
        scale = max(abs(old_number), abs(new_number), 1.0)
        return difference <= 1e-9 * scale
    return str(old_value).strip() == str(new_value).strip()


def describe_change(old_value, new_value, blocked=False):
    """Short human label for what a single write would do.

    Returns one of: "blocked", "no change", "set (was empty)",
    "increase", "decrease", "replaced".
    """
    if blocked:
        return "blocked"
    if new_value is None:
        return "blocked"
    if old_value is None or (isinstance(old_value, STRING_TYPES) and not old_value.strip()):
        return "set (was empty)"
    if values_equal(old_value, new_value):
        return "no change"
    old_number = coerce_number(old_value)
    new_number = coerce_number(new_value)
    if old_number is not None and new_number is not None:
        return "increase" if new_number > old_number else "decrease"
    return "replaced"


def build_transfer_report(matches, param_map):
    """Flatten a dry run into one row per (element, parameter) write.

    Each row is exactly one prospective write, so the caller can show a
    literal before/after table rather than a summary. Only the matches
    handed in are reported -- pass in the already-filtered set and the
    report can't disagree with what the transfer will actually do.

    Row keys: target, source, target_label, source_label, match_value,
    source_param, target_param, old_value, new_value, delta, will_write,
    issue, warning, change.
    """
    previews = preview_transfer(matches, param_map)

    rows = []
    for match, preview in zip(matches, previews):
        target = match["target"]
        source = match["source"]

        for entry in preview["previews"]:
            old_value = entry["current_value"]
            new_value = entry["new_value"]
            will_write = entry["will_write"]

            delta = None
            if will_write:
                old_number = coerce_number(old_value)
                new_number = coerce_number(new_value)
                if old_number is not None and new_number is not None:
                    delta = new_number - old_number

            rows.append({
                "target": target,
                "source": source,
                "target_label": element_label(target),
                "source_label": element_label(source),
                "match_value": match.get("key") or "",
                "source_param": entry["source_param"],
                "target_param": entry["target_param"],
                "old_value": old_value,
                "new_value": new_value,
                "delta": delta,
                "will_write": will_write,
                "issue": entry["issue"],
                "warning": entry.get("warning"),
                "change": describe_change(old_value, new_value, blocked=not will_write),
            })
    return rows


def report_totals(rows):
    """Counts for the report header: {writes, blocked, unchanged, elements}."""
    writes = 0
    blocked = 0
    unchanged = 0
    elements = set()

    for row in rows:
        if row["target"] is not None:
            elements.add(id_number(row["target"]))
        if not row["will_write"]:
            blocked += 1
        elif row["change"] == "no change":
            unchanged += 1
        else:
            writes += 1

    return {
        "writes": writes,
        "blocked": blocked,
        "unchanged": unchanged,
        "elements": len(elements),
    }


# ---------------------------------------------------------------------------
# Transfer
# ---------------------------------------------------------------------------

def transfer(doc, matches, param_map):
    """Copy mapped parameter values from source to target for every matched
    pair, inside a single transaction. Ambiguous/unmatched targets are
    skipped (match["matched"] is False for them).

    param_map: list of (source_param_name, target_param_name, multiplier)
    tuples. multiplier is applied to numeric values only (e.g. 0.001 to
    convert Watts to kW); it's ignored for text values.

    The writes are taken from preview_transfer(), so the transfer does EXACTLY what the review
    window showed: a row shown BLOCKED (read-only, missing, TYPE-bound target, no source value)
    is skipped, and a value that is already correct is not written again.

    Returns {"updated": int, "values": int, "unchanged": int, "skipped": int, "errors": [str, ...]}.
    """
    pairs = [(e[0], e[1], e[2] if len(e) > 2 else 1.0) for e in param_map]
    previews = preview_transfer(matches, pairs)
    updated = written = unchanged = skipped = 0
    errors = []

    t = Transaction(doc, "Transfer Load Data to Detail Items")
    t.Start()
    try:
        for match, preview in zip(matches, previews):
            if not match["matched"]:
                continue
            target = match["target"]
            changed_this_target = False

            for entry in preview["previews"]:
                if not entry["will_write"]:
                    skipped += 1
                    continue
                if values_equal(entry["current_value"], entry["new_value"]):
                    unchanged += 1
                    continue
                ok, err = set_param_value(target, entry["target_param"], entry["new_value"])
                if ok:
                    changed_this_target = True
                    written += 1
                else:
                    errors.append(
                        "Id {}: '{}' -> '{}' failed ({})".format(
                            id_number(target), entry["source_param"], entry["target_param"], err
                        )
                    )

            if changed_this_target:
                updated += 1

        status = t.Commit()
    except Exception:
        # Only roll back a transaction that is still open: when Commit() itself throws, the
        # transaction is already finished and RollBack() would raise a second error that hides
        # the real one.
        if t.HasStarted() and not t.HasEnded():
            t.RollBack()
        raise

    # Revit can roll the whole transaction back during Commit (an error-level failure while
    # regenerating). Nothing was saved then -- never report the counted writes as done.
    if status != TransactionStatus.Committed:
        errors.insert(0, "Revit did not commit the transaction (status: {}) - NOTHING was written. "
                         "Check the Revit warnings / errors and try again.".format(status))
        return {"updated": 0, "values": 0, "unchanged": unchanged, "skipped": skipped,
                "errors": errors}

    return {"updated": updated, "values": written, "unchanged": unchanged, "skipped": skipped,
            "errors": errors}
