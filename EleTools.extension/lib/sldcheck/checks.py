# -*- coding: utf-8 -*-
"""SLD vs model comparison. Pure Python - works on plain record dicts made
by collect.py (or by the unit tests), never touches the Revit API.

Detail record (SLD):
  {'id': int, 'role': 'equipment'|'breaker'|'cable'|None, 'label': 'Fam : Type',
   'view': 'view name', 'view_id': int, 'f': {field: text}}
Model record:
  {'id': int, 'kind': 'equipment'|'circuit'|'fixture', 'label': str,
   'part_type': str, 'f': {field: text}, 'n': {field: float in A / kVA / kW},
   circuits also: 'source_panel': str, 'downstream': [panel names],
                  'is_spare': bool, 'is_space': bool}

Matching rule: the normalised Panel / Equipment name is the key. A key that
hits more than one model element is reported as AMBIGUOUS and is never
compared - nothing is matched silently.
"""
from sldcheck import rules

C_MATCH = u"1. Matching"
C_PANEL = u"2. Panels & Equipment"
C_CIRCUIT = u"3. Circuits & Connectivity"
C_BREAKER = u"4. Breakers"
C_CABLE = u"5. Cables"
C_LOAD = u"6. Loads"
CHECK_ORDER = [C_MATCH, C_PANEL, C_CIRCUIT, C_BREAKER, C_CABLE, C_LOAD]

ERROR, WARNING, INFO = u"ERROR", u"WARNING", u"INFO"
SEVERITY_ORDER = {ERROR: 0, WARNING: 1, INFO: 2}


# Switchable checks shown in the setup window: (key, group, label, locked).
# Locked ones are always on - duplicates / ambiguous names are never hidden,
# so a wrong match can never pass silently.
CHECK_ITEMS = [
    (u"match_duplicates", C_MATCH, u"Duplicate Detail Items / duplicate panel names", True),
    (u"match_ambiguous", C_MATCH, u"Ambiguous matches (reported, never compared)", True),
    (u"match_missing_model", C_MATCH, u"Missing model element (on SLD, not in model)", False),
    (u"match_missing_detail", C_MATCH, u"Missing Detail Item (in model, not on SLD)", False),
    (u"match_multi_view", C_MATCH, u"Same board / feeder drawn in several views", False),
    (u"match_unnamed", C_MATCH, u"Boards without a name (SLD or model)", False),
    (u"panel_name_format", C_PANEL, u"Panel name formatting (case / spaces)", False),
    (u"panel_equipment_name", C_PANEL, u"Equipment name / description", False),
    (u"panel_classification", C_PANEL, u"Classification MDB / SMDB / DB", False),
    (u"panel_transformer", C_PANEL, u"Transformer identification", False),
    (u"circuit_fed_from", C_CIRCUIT, u"Source panel (Fed From vs Supply From)", False),
    (u"circuit_missing", C_CIRCUIT, u"SLD feeder without a model circuit", False),
    (u"circuit_number", C_CIRCUIT, u"Circuit number", False),
    (u"circuit_feeds", C_CIRCUIT, u"Downstream panel / load of the circuit", False),
    (u"circuit_outgoing", C_CIRCUIT, u"Outgoing feeder connected to the board", False),
    (u"circuit_symbols", C_CIRCUIT, u"Breaker and cable symbols agree", False),
    (u"circuit_incomplete", C_CIRCUIT, u"Feeder symbols without source / load", False),
    (u"breaker_rating", C_BREAKER, u"Breaker rating", False),
    (u"breaker_frame", C_BREAKER, u"Breaker frame", False),
    (u"breaker_vs_current", C_BREAKER, u"Breaker rating vs circuit design current", False),
    (u"breaker_poles", C_BREAKER, u"Number of poles", False),
    (u"breaker_spare", C_BREAKER, u"Spare breaker identification", False),
    (u"breaker_spare_count", C_BREAKER, u"Spare count per board", False),
    (u"cable_size", C_CABLE, u"Cable size", False),
    (u"cable_cores", C_CABLE, u"Number of cores", False),
    (u"cable_type", C_CABLE, u"Cable type", False),
    (u"cable_desc", C_CABLE, u"Cable description", False),
    (u"load_connected", C_LOAD, u"Total connected load", False),
    (u"load_demand", C_LOAD, u"Maximum demand load", False),
    (u"load_demand_vs_connected", C_LOAD, u"Maximum demand above connected load", False),
]

# issue text prefix -> check key (first match wins; no match = always on)
ISSUE_KEYS = [
    (u"Duplicate", u"match_duplicates"), (u"Ambiguous", u"match_ambiguous"),
    (u"Missing model element", u"match_missing_model"),
    (u"Missing circuit in model", u"circuit_missing"),
    (u"Missing Detail Item", u"match_missing_detail"),
    (u"Drawn in several views", u"match_multi_view"),
    (u"Feeder drawn in several views", u"match_multi_view"),
    (u"Board sections", u"match_multi_view"),
    (u"Feeder sections", u"match_multi_view"),
    (u"Model equipment without name", u"match_unnamed"),
    (u"SLD board without name", u"match_unnamed"),
    (u"Name formatting", u"panel_name_format"),
    (u"Equipment name", u"panel_equipment_name"),
    (u"Classification", u"panel_classification"),
    (u"Transformer", u"panel_transformer"),
    (u"Source panel", u"circuit_fed_from"), (u"Not connected in model", u"circuit_fed_from"),
    (u"Circuit feeds", u"circuit_feeds"), (u"Outgoing feeder", u"circuit_outgoing"),
    (u"Circuit number", u"circuit_number"), (u"SLD symbols disagree", u"circuit_symbols"),
    (u"Feeder without", u"circuit_incomplete"),
    (u"Spare identification", u"breaker_spare"), (u"Spare count", u"breaker_spare_count"),
    (u"Breaker rating", u"breaker_rating"), (u"Breaker frame", u"breaker_frame"),
    (u"Rating below", u"breaker_vs_current"), (u"Number of poles", u"breaker_poles"),
    (u"Cable size", u"cable_size"), (u"No cable data", u"cable_size"),
    (u"Number of cores", u"cable_cores"), (u"Cable type", u"cable_type"),
    (u"Cable description", u"cable_desc"),
    (u"Total connected load", u"load_connected"), (u"Connected load", u"load_connected"),
    (u"Maximum demand", u"load_demand"), (u"Demand above", u"load_demand_vs_connected"),
]
_LOCKED = set(item[0] for item in CHECK_ITEMS if item[3])


def issue_key(issue):
    for prefix, key in ISSUE_KEYS:
        if issue.startswith(prefix):
            return key
    return None


def enabled_checks(cfg):
    """{key: bool} - every CHECK_ITEMS key, locked ones always True."""
    chosen = cfg.get("checks") or {}
    return dict((key, True if key in _LOCKED else bool(chosen.get(key, True)))
                for key, _group, _label, _locked in CHECK_ITEMS)


class Findings(object):
    def __init__(self, enabled=None, show_info=True):
        self.items = []
        self.ok = dict((c, 0) for c in CHECK_ORDER)      # passed comparisons
        self.enabled = enabled or {}
        self.show_info = show_info
        self.hidden = 0

    def add(self, check, severity, issue, key, message, sld=u"", model=u"",
            sld_ids=(), model_ids=(), view=u""):
        toggle = issue_key(issue)
        if (toggle and not self.enabled.get(toggle, True)) or \
                (severity == INFO and not self.show_info):
            self.hidden += 1
            return
        self.items.append({
            "check": check, "severity": severity, "issue": issue,
            "key": rules.to_text(key), "message": message,
            "sld": rules.to_text(sld), "model": rules.to_text(model),
            "sld_ids": [i for i in sld_ids if i is not None],
            "model_ids": [i for i in model_ids if i is not None],
            "view": rules.to_text(view)})

    def passed(self, check):
        self.ok[check] += 1

    def sorted(self):
        return sorted(self.items, key=lambda f: (CHECK_ORDER.index(f["check"]),
                                                 SEVERITY_ORDER[f["severity"]],
                                                 rules.norm(f["key"]), f["issue"]))


def _f(rec, field):
    return rules.to_text((rec.get("f") or {}).get(field)).strip()


def _n(rec, field):
    return (rec.get("n") or {}).get(field)


def _ids(recs):
    return [r["id"] for r in recs]


def _views(recs):
    return u", ".join(sorted(set(r.get("view") or u"" for r in recs)))


# ----------------------------------------------------------------------
# Generic comparisons
# ----------------------------------------------------------------------
def _compare_text(out, check, issue, key, sld_text, model_text, d_ids, m_ids, view,
                  severity=WARNING):
    if not sld_text or not model_text:
        return
    if rules.norm(sld_text) == rules.norm(model_text):
        out.passed(check)
    else:
        out.add(check, severity, issue, key,
                u"SLD shows '{0}', model has '{1}'.".format(sld_text, model_text),
                sld_text, model_text, d_ids, m_ids, view)


def _compare_load(out, cfg, key, field, label, sld_text, model, d_ids, view):
    """SLD load text vs model value (kVA, or kW when the SLD says kW)."""
    if not sld_text:
        return
    value, unit = rules.parse_power(sld_text, cfg.get("default_load_unit", u"kVA"))
    if value is None:
        out.add(C_LOAD, INFO, label + u" not readable", key,
                u"SLD value '{0}' has no number.".format(sld_text),
                sld_text, u"", d_ids, [model["id"]], view)
        return
    model_unit = cfg["units"].get(field, u"kVA")
    model_value = _n(model, field)
    if unit != model_unit:
        alt = _n(model, field + u"_kw") if unit == u"kW" else None
        if alt is None:
            out.add(C_LOAD, INFO, label + u" unit differs", key,
                    u"SLD gives {0} {1}; model value is in {2} - not compared."
                    .format(rules.fmt_num(value), unit, model_unit),
                    sld_text, rules.fmt_num(model_value), d_ids, [model["id"]], view)
            return
        model_value, model_unit = alt, unit
    if model_value is None:
        out.add(C_LOAD, INFO, label + u" not in model", key,
                u"Model element has no {0} value to compare.".format(label.lower()),
                sld_text, u"", d_ids, [model["id"]], view)
        return
    if rules.within(value, model_value, pct_tol=cfg["load_tolerance_pct"], abs_tol=0.01):
        out.passed(C_LOAD)
        return
    out.add(C_LOAD, WARNING, label + u" differs", key,
            u"SLD {0} {2}, model {1} {2} (tolerance {3} %).".format(
                rules.fmt_num(value), rules.fmt_num(model_value), unit,
                rules.fmt_num(cfg["load_tolerance_pct"])),
            u"{0} {1}".format(rules.fmt_num(value), unit),
            u"{0} {1}".format(rules.fmt_num(model_value), model_unit),
            d_ids, [model["id"]], view)


def _hint(loose_index, name):
    hits = loose_index.get(rules.loose(name), [])
    if len(hits) == 1:
        return u" Closest model name: '{0}' (spacing / punctuation differs).".format(
            _f(hits[0], "name"))
    if len(hits) > 1:
        return u" {0} model names differ only in punctuation.".format(len(hits))
    return u""


# ----------------------------------------------------------------------
# Equipment / panels
# ----------------------------------------------------------------------
def classify_detail(rec, cfg):
    rules_list = cfg["classification_rules"]
    explicit = _f(rec, "classification")
    if explicit:
        return rules.classify([explicit], rules_list) or explicit.upper()
    return rules.classify([_f(rec, "name"), rec.get("label")], rules_list)


def classify_model(rec, cfg):
    rules_list = cfg["classification_rules"]
    explicit = _f(rec, "classification")
    if explicit:
        return rules.classify([explicit], rules_list) or explicit.upper()
    if rules.norm(rec.get("part_type")) == u"transformer":
        return u"TX"
    return rules.classify([_f(rec, "name"), rec.get("label"), rec.get("part_type")],
                          rules_list)


def _compare_equipment(out, cfg, d, m):
    key, view, d_ids, m_ids = _f(d, "name"), d.get("view"), [d["id"]], [m["id"]]
    m_name = _f(m, "name")
    if key != m_name:
        out.add(C_PANEL, INFO, u"Name formatting differs", key,
                u"Matched ignoring case/spaces: SLD '{0}' vs model '{1}'.".format(key, m_name),
                key, m_name, d_ids, m_ids, view)
    else:
        out.passed(C_PANEL)
    _compare_text(out, C_PANEL, u"Equipment name differs", key, _f(d, "equipment_name"),
                  _f(m, "equipment_name"), d_ids, m_ids, view)

    if m.get("kind") == "equipment":
        d_class, m_class = classify_detail(d, cfg), classify_model(m, cfg)
        if d_class and m_class:
            if d_class == m_class:
                out.passed(C_PANEL)
            elif u"TX" in (d_class, m_class):
                out.add(C_PANEL, ERROR, u"Transformer identification", key,
                        u"SLD treats it as {0}, model as {1}.".format(d_class, m_class),
                        d_class, m_class, d_ids, m_ids, view)
            else:
                out.add(C_PANEL, WARNING, u"Classification differs", key,
                        u"SLD {0} vs model {1} (MDB / SMDB / DB).".format(d_class, m_class),
                        d_class, m_class, d_ids, m_ids, view)
        elif not m_class:
            out.add(C_PANEL, INFO, u"Classification unknown", key,
                    u"Model equipment could not be classified as TX / MDB / SMDB / DB "
                    u"(check classification_rules).", d_class or u"", u"", d_ids, m_ids, view)

    # Connectivity: the SLD 'Fed From' against the model 'Supply From'
    d_from, m_from = _f(d, "fed_from"), _f(m, "fed_from")
    if d_from and m_from:
        if rules.norm(d_from) == rules.norm(m_from):
            out.passed(C_CIRCUIT)
        else:
            out.add(C_CIRCUIT, ERROR, u"Source panel differs", key,
                    u"SLD: fed from '{0}'; model: supplied from '{1}'.".format(d_from, m_from),
                    d_from, m_from, d_ids, m_ids, view)
    elif d_from and m.get("kind") == "equipment":
        out.add(C_CIRCUIT, WARNING, u"Not connected in model", key,
                u"SLD shows it fed from '{0}' but the model element has no Supply From "
                u"(not connected to a circuit).".format(d_from), d_from, u"", d_ids, m_ids, view)

    _compare_load(out, cfg, key, u"connected_load", u"Total connected load",
                  _f(d, "connected_load"), m, d_ids, view)
    _compare_load(out, cfg, key, u"demand_load", u"Maximum demand",
                  _f(d, "demand_load"), m, d_ids, view)
    connected = rules.parse_power(_f(d, "connected_load"), cfg["default_load_unit"])
    demand = rules.parse_power(_f(d, "demand_load"), cfg["default_load_unit"])
    if (connected[0] is not None and demand[0] is not None and connected[1] == demand[1]
            and demand[0] > connected[0] * (1 + cfg["load_tolerance_pct"] / 100.0)):
        out.add(C_LOAD, WARNING, u"Demand above connected load", key,
                u"SLD maximum demand {0} exceeds connected load {1}.".format(
                    _f(d, "demand_load"), _f(d, "connected_load")),
                _f(d, "demand_load"), u"", d_ids, m_ids, view)


# ----------------------------------------------------------------------
# Feeders (breaker + cable detail items) vs electrical circuits
# ----------------------------------------------------------------------
FEEDER_FIELDS = ["fed_from", "feeder", "circuit_number", "breaker_ref", "breaker_rating",
                 "breaker_frame", "poles", "spare", "cable_type", "cable_size", "cores",
                 "cable_desc", "connected_load", "demand_load"]


def is_spare_detail(rec, cfg):
    words = cfg["spare_keywords"]
    return (rules.is_truthy(_f(rec, "spare")) or rules.has_keyword(_f(rec, "spare"), words)
            or rules.has_keyword(_f(rec, "feeder"), words))


def _merge_group(out, members, key):
    """One logical feeder from its breaker + cable symbols."""
    merged = {}
    for field in FEEDER_FIELDS:
        values = []
        for rec in members:
            value = _f(rec, field)
            if value and rules.norm(value) not in [rules.norm(v) for v in values]:
                values.append(value)
        if values:
            merged[field] = values[0]
        if len(values) > 1:
            out.add(C_CIRCUIT, WARNING, u"SLD symbols disagree", key,
                    u"The breaker/cable symbols of this feeder give different '{0}': {1}."
                    .format(field, u" | ".join(values)), u" | ".join(values), u"",
                    _ids(members), [], _views(members))
    return merged


def _circuit_targets(circuit):
    names = [rules.norm(n) for n in circuit.get("downstream") or [] if n]
    load = rules.norm(_f(circuit, "load_name"))
    if load and load not in names:
        names.append(load)
    return names


def _cable_parts(text_by_field):
    """cores / size / type from the dedicated fields first, then free text."""
    size_text = text_by_field.get("cable_size") or u""
    desc = text_by_field.get("cable_desc") or u""
    parsed_size, parsed_desc = rules.parse_cable(size_text), rules.parse_cable(desc)
    cores = rules.first_number(text_by_field.get("cores")) if text_by_field.get("cores") else None
    if cores is None:
        cores = parsed_size["cores"] if parsed_size["cores"] is not None else parsed_desc["cores"]
    size = parsed_size["size_mm2"]
    if size is None and size_text:
        size = rules.first_number(size_text)
    if size is None:
        size = parsed_desc["size_mm2"]
    ctype = text_by_field.get("cable_type") or u""
    return cores, size, ctype, desc


def _compare_feeder(out, cfg, key, merged, members, circuit, eq_index, matched_by_number):
    d_ids, m_ids, view = _ids(members), [circuit["id"]], _views(members)
    to = merged.get("feeder", u"")
    targets = _circuit_targets(circuit)
    downstream = circuit.get("downstream") or []

    # --- connectivity / outgoing feeder -----------------------------------
    if matched_by_number and to and not is_spare_detail({"f": merged}, cfg):
        if rules.norm(to) in targets:
            out.passed(C_CIRCUIT)
        else:
            out.add(C_CIRCUIT, ERROR, u"Circuit feeds a different load", key,
                    u"SLD circuit {0} feeds '{1}'; model circuit feeds '{2}'.".format(
                        merged.get("circuit_number"), to,
                        u", ".join(downstream) or _f(circuit, "load_name")),
                    to, u", ".join(downstream) or _f(circuit, "load_name"), d_ids, m_ids, view)
    if to and rules.norm(to) in eq_index and not downstream:
        out.add(C_CIRCUIT, WARNING, u"Outgoing feeder not connected", key,
                u"SLD shows an outgoing feeder to board '{0}', but the model circuit is not "
                u"connected to that equipment (only the load name matches).".format(to),
                to, _f(circuit, "load_name"), d_ids, m_ids, view)
    d_num, m_num = merged.get("circuit_number"), _f(circuit, "circuit_number")
    if d_num and m_num and not matched_by_number:
        _compare_text(out, C_CIRCUIT, u"Circuit number differs", key, d_num, m_num,
                      d_ids, m_ids, view)

    # --- breaker ------------------------------------------------------------
    d_spare = is_spare_detail({"f": merged}, cfg)
    m_spare = bool(circuit.get("is_spare") or circuit.get("is_space"))
    if d_spare != m_spare:
        out.add(C_BREAKER, WARNING, u"Spare identification", key,
                u"SLD marks it {0}; model circuit is {1}.".format(
                    u"SPARE" if d_spare else u"a used way",
                    u"SPARE/SPACE" if m_spare else u"a used circuit"),
                u"spare" if d_spare else u"used", u"spare" if m_spare else u"used",
                d_ids, m_ids, view)
    else:
        out.passed(C_BREAKER)

    tol = cfg["rating_tolerance_a"]
    for field, label, severity in (("breaker_rating", u"Breaker rating", ERROR),
                                   ("breaker_frame", u"Breaker frame", WARNING)):
        d_val = rules.parse_current(merged.get(field)) if merged.get(field) else None
        m_val = _n(circuit, field)
        if d_val is None or m_val is None:
            continue
        if rules.within(d_val, m_val, abs_tol=tol):
            out.passed(C_BREAKER)
        else:
            out.add(C_BREAKER, severity, label + u" differs", key,
                    u"SLD {0} A, model {1} A.".format(rules.fmt_num(d_val), rules.fmt_num(m_val)),
                    merged.get(field), u"{0} A".format(rules.fmt_num(m_val)), d_ids, m_ids, view)

    rating = _n(circuit, "breaker_rating")
    if rating is None and merged.get("breaker_rating"):
        rating = rules.parse_current(merged.get("breaker_rating"))
    current = _n(circuit, "design_current")
    if rating and current and current > rating + tol:
        out.add(C_BREAKER, WARNING, u"Rating below circuit current", key,
                u"Circuit design current {0} A exceeds breaker rating {1} A.".format(
                    rules.fmt_num(current), rules.fmt_num(rating)),
                merged.get("breaker_rating", u""), u"{0} A".format(rules.fmt_num(current)),
                d_ids, m_ids, view)

    d_poles = rules.parse_poles(merged.get("poles"))
    m_poles = _n(circuit, "poles")
    if d_poles is not None and m_poles is not None:
        if int(round(m_poles)) == d_poles:
            out.passed(C_BREAKER)
        else:
            out.add(C_BREAKER, ERROR, u"Number of poles differs", key,
                    u"SLD '{0}' ({1} pole) vs model {2} pole.".format(
                        merged.get("poles"), d_poles, int(round(m_poles))),
                    merged.get("poles"), rules.fmt_num(m_poles), d_ids, m_ids, view)

    # --- cable --------------------------------------------------------------
    d_cores, d_size, d_type, d_desc = _cable_parts(merged)
    m_fields = dict((k, _f(circuit, k)) for k in ("cable_size", "cable_desc", "cores",
                                                  "cable_type"))
    m_cores, m_size, m_type, m_desc = _cable_parts(m_fields)
    has_d_cable = any(v not in (None, u"") for v in (d_cores, d_size, d_type, d_desc))
    has_m_cable = any(v not in (None, u"") for v in (m_cores, m_size, m_type, m_desc))
    if has_d_cable and not has_m_cable:
        out.add(C_CABLE, INFO, u"No cable data in model", key,
                u"SLD gives cable data but the model circuit has none - not compared.",
                merged.get("cable_size") or d_desc, u"", d_ids, m_ids, view)
    elif has_d_cable:
        if d_size is not None and m_size is not None:
            if rules.within(d_size, m_size, abs_tol=cfg["cable_size_tolerance_mm2"]):
                out.passed(C_CABLE)
            else:
                out.add(C_CABLE, ERROR, u"Cable size differs", key,
                        u"SLD {0} mm², model {1} mm².".format(
                            rules.fmt_num(d_size), rules.fmt_num(m_size)),
                        merged.get("cable_size") or d_desc, m_fields["cable_size"] or m_desc,
                        d_ids, m_ids, view)
        if d_cores is not None and m_cores is not None:
            if rules.within(d_cores, m_cores, abs_tol=0.01):
                out.passed(C_CABLE)
            else:
                out.add(C_CABLE, ERROR, u"Number of cores differs", key,
                        u"SLD {0} core, model {1} core.".format(
                            rules.fmt_num(d_cores), rules.fmt_num(m_cores)),
                        rules.fmt_num(d_cores), rules.fmt_num(m_cores), d_ids, m_ids, view)
        if d_type and m_type:
            if rules.cable_type_key(d_type) == rules.cable_type_key(m_type):
                out.passed(C_CABLE)
            else:
                out.add(C_CABLE, WARNING, u"Cable type differs", key,
                        u"SLD '{0}', model '{1}'.".format(d_type, m_type),
                        d_type, m_type, d_ids, m_ids, view)
        if d_desc and m_desc:
            _compare_text(out, C_CABLE, u"Cable description differs", key, d_desc, m_desc,
                          d_ids, m_ids, view, severity=INFO)

    # --- load ---------------------------------------------------------------
    _compare_load(out, cfg, key, u"connected_load", u"Connected load",
                  merged.get("connected_load"), circuit, d_ids, view)
    _compare_load(out, cfg, key, u"demand_load", u"Maximum demand",
                  merged.get("demand_load"), circuit, d_ids, view)


# ----------------------------------------------------------------------
# Main entry
# ----------------------------------------------------------------------
def _model_sections(recs, cfg):
    """True when same-name model elements are sections of ONE board: same
    family/type, same document (host or one link) and all within
    section_merge_distance_m of the first."""
    if not cfg.get("merge_board_sections"):
        return False
    if len(set(r.get("type_label") or r.get("label") for r in recs)) != 1:
        return False
    if len(set(bool(r.get("linked")) for r in recs)) != 1:
        return False
    limit = float(cfg.get("section_merge_distance_m", 10.0) or 0.0)
    first = recs[0].get("xyz")
    if first is None:
        return False
    for rec in recs[1:]:
        point = rec.get("xyz")
        if point is None:
            return False
        dist = sum((a - b) ** 2 for a, b in zip(first, point)) ** 0.5
        if dist > limit:
            return False
    return True


def _strip_suffix(name, cfg):
    """Name without the first matching 'name_suffix_patterns' ending, or None."""
    import re
    for pattern in cfg.get("name_suffix_patterns") or []:
        try:
            base = re.sub(pattern, u"", name).strip()
        except re.error:
            continue
        if base and base != name.strip():
            return base
    return None


def _feeder_pieces_agree(recs):
    """Same feeder drawn in several pieces: every filled feeder field agrees."""
    for field in FEEDER_FIELDS:
        values = set(rules.norm(_f(r, field)) for r in recs if _f(r, field))
        if len(values) > 1:
            return False
    return True


def _sld_sections(recs, cfg):
    """True when same-name symbols in ONE view are sections of one board: same
    family/type and drawn next to each other (within sld_section_distance_mm
    on paper of the first symbol). Far-apart copies stay duplicates."""
    if not cfg.get("merge_board_sections"):
        return False
    if len(set(r.get("label") for r in recs)) != 1:
        return False
    limit = float(cfg.get("sld_section_distance_mm", 60.0) or 0.0)
    first = recs[0].get("paper_xy")
    if first is None:
        return False
    for rec in recs[1:]:
        point = rec.get("paper_xy")
        if point is None:
            return False
        if ((first[0] - point[0]) ** 2 + (first[1] - point[1]) ** 2) ** 0.5 > limit:
            return False
    return True


def run_checks(details, models, cfg, whole_project=True):
    """Returns (Findings, stats)."""
    out = Findings(enabled_checks(cfg), cfg.get("show_info", True))
    stats = {"sld_equipment": 0, "sld_feeders": 0, "sld_ignored": 0,
             "model_equipment": 0, "model_circuits": 0, "model_fixtures": 0,
             "matched_equipment": 0, "matched_feeders": 0, "outgoing_feeders": 0,
             "feeders_by_panel": {}}

    # ---- model indexes ---------------------------------------------------
    eq_index, fx_index, loose_index = {}, {}, {}
    circuits, unnamed_model, unnamed_sld = [], {}, {}
    for rec in models:
        kind = rec.get("kind")
        if kind == "circuit":
            circuits.append(rec)
            stats["model_circuits"] += 1
            continue
        name = _f(rec, "name")
        stats["model_" + ("equipment" if kind == "equipment" else "fixtures")] += 1
        if not name:
            if kind == "equipment":
                unnamed_model.setdefault(rec.get("label") or u"<element>", []).append(rec)
            continue
        (eq_index if kind == "equipment" else fx_index).setdefault(rules.norm(name), []).append(rec)
        loose_index.setdefault(rules.loose(name), []).append(rec)

    # one row per family type: battery racks, trenches, UPS parts ... are often
    # equipment without a Panel Name on purpose - INFO, not one warning each
    for label, recs in sorted(unnamed_model.items()):
        out.add(C_MATCH, INFO, u"Model equipment without name", u"<no name>",
                u"{0} element(s) of this type have no Panel Name - they cannot be matched "
                u"(fine for racks, trenches, accessories).".format(len(recs)),
                u"", label, [], _ids(recs))

    for key, recs in sorted(eq_index.items()):
        if len(recs) > 1 and _model_sections(recs, cfg):
            # one board built from several sections - keep the first as the board
            eq_index[key] = [recs[0]]
            out.add(C_MATCH, INFO, u"Board sections (model)", _f(recs[0], "name"),
                    u"{0} elements of {1} share this name within {2} m - treated as one board."
                    .format(len(recs), recs[0].get("type_label") or recs[0].get("label"),
                            rules.fmt_num(cfg.get("section_merge_distance_m", 10.0))),
                    u"", recs[0].get("label"), [], _ids(recs))
        elif len(recs) > 1:
            out.add(C_MATCH, ERROR, u"Duplicate panel name", _f(recs[0], "name"),
                    u"{0} model equipment elements share this Panel Name - SLD items with "
                    u"this name are reported as ambiguous.".format(len(recs)),
                    u"", u", ".join(r.get("label") or u"" for r in recs), [], _ids(recs))

    # ---- SLD detail items ----------------------------------------------------
    eq_details, feeder_details = [], []
    for rec in details:
        role = rec.get("role")
        if role == "equipment":
            if _f(rec, "name"):
                eq_details.append(rec)
            else:
                unnamed_sld.setdefault((rec.get("label"), rec.get("view")), []).append(rec)
        elif role in ("breaker", "cable"):
            feeder_details.append(rec)
        else:
            stats["sld_ignored"] += 1
    stats["sld_equipment"], stats["sld_feeders"] = len(eq_details), len(feeder_details)
    for (label, view), recs in sorted(unnamed_sld.items(),
                                      key=lambda kv: (kv[0][1] or u"", kv[0][0] or u"")):
        out.add(C_MATCH, WARNING, u"SLD board without name", u"<no name>",
                u"{0} board / equipment symbol(s) of this type have no name value - they cannot "
                u"be matched. If this family is not a board, add it to 'Ignore these symbols'."
                .format(len(recs)), label, u"", _ids(recs), [], view)

    by_name = {}
    for rec in eq_details:
        by_name.setdefault(rules.norm(_f(rec, "name")), []).append(rec)

    for key, recs in sorted(by_name.items()):
        per_view = {}
        for rec in recs:
            per_view.setdefault(rec.get("view_id"), []).append(rec)
        for view_recs in per_view.values():
            if len(view_recs) > 1 and _sld_sections(view_recs, cfg):
                out.add(C_MATCH, INFO, u"Board sections (SLD)", _f(view_recs[0], "name"),
                        u"{0} '{1}' symbols with this name in one view - treated as one board."
                        .format(len(view_recs), view_recs[0].get("label")),
                        u"", u"", _ids(view_recs), [], _views(view_recs))
            elif len(view_recs) > 1:
                out.add(C_MATCH, ERROR, u"Duplicate Detail Item", _f(view_recs[0], "name"),
                        u"{0} board symbols with this name in the same view{1}.".format(
                            len(view_recs), u"" if cfg.get("merge_board_sections") else
                            u" (section merging is off)"),
                        u", ".join(sorted(set(r.get("label") or u"" for r in view_recs))), u"",
                        _ids(view_recs), [], _views(view_recs))
        if len(per_view) > 1:
            allowed = cfg["allow_same_name_in_different_views"]
            out.add(C_MATCH, INFO if allowed else ERROR,
                    u"Drawn in several views" if allowed else u"Duplicate Detail Item",
                    _f(recs[0], "name"),
                    u"Board appears in {0} views: {1}.".format(len(per_view), _views(recs)),
                    u"", u"", _ids(recs), [], _views(recs))

    # ---- equipment matching -----------------------------------------------------
    matched_models = set()
    for key, recs in sorted(by_name.items()):
        candidates = eq_index.get(key, []) + fx_index.get(key, [])
        name = _f(recs[0], "name")
        if not candidates:
            # 'PP-01(E)' -> 'PP-01': only as a fallback, and always reported
            base = _strip_suffix(name, cfg)
            if base:
                candidates = eq_index.get(rules.norm(base), []) + fx_index.get(rules.norm(base), [])
                if len(candidates) == 1:
                    out.add(C_MATCH, WARNING, u"Matched by base name", name,
                            u"No model element named '{0}' - matched to '{1}' after removing the "
                            u"name ending.".format(name, _f(candidates[0], "name")),
                            name, _f(candidates[0], "name"), _ids(recs), _ids(candidates),
                            _views(recs))
        if not candidates:
            out.add(C_MATCH, ERROR, u"Missing model element", name,
                    u"No electrical equipment / fixture with this name in the model."
                    + _hint(loose_index, name), name, u"", _ids(recs), [], _views(recs))
            continue
        if len(candidates) > 1:
            out.add(C_MATCH, ERROR, u"Ambiguous match", name,
                    u"{0} model elements have this name ({1}) - not compared.".format(
                        len(candidates), u", ".join(sorted(set(c.get("kind") for c in candidates)))),
                    name, u", ".join(c.get("label") or u"" for c in candidates),
                    _ids(recs), _ids(candidates), _views(recs))
            continue
        model = candidates[0]
        matched_models.add(model["id"])
        stats["matched_equipment"] += 1
        out.passed(C_MATCH)
        for rec in recs:
            _compare_equipment(out, cfg, rec, model)

    drawn = set(by_name.keys())
    for rec in feeder_details:
        if _f(rec, "fed_from"):
            drawn.add(rules.norm(_f(rec, "fed_from")))

    for key, recs in sorted(eq_index.items()):
        if any(r["id"] in matched_models for r in recs) or key in by_name:
            continue
        rec = recs[0]
        upstream = rules.norm(_f(rec, "fed_from"))
        # linked models usually hold other zones / packages: only report their
        # boards when the source board is drawn (same rule as a partial scope)
        if (whole_project and not rec.get("linked")) or (upstream and upstream in drawn):
            out.add(C_MATCH, ERROR, u"Missing Detail Item", _f(rec, "name"),
                    u"Model equipment is not drawn on the SLD{0}.".format(
                        u" (fed from '{0}', which is drawn)".format(_f(rec, "fed_from"))
                        if upstream in drawn and upstream else u""),
                    u"", rec.get("label"), [], _ids(recs))

    # ---- feeders ---------------------------------------------------------------
    groups, order = {}, []
    for rec in feeder_details:
        src = rules.norm(_f(rec, "fed_from"))
        if not src:
            out.add(C_CIRCUIT, WARNING, u"Feeder without source", _f(rec, "feeder") or u"<blank>",
                    u"Breaker / cable symbol has no Fed From value - cannot find its circuit.",
                    rec.get("label"), u"", [rec["id"]], [], rec.get("view"))
            continue
        to = rules.norm(_f(rec, "feeder"))
        num = rules.norm(_f(rec, "circuit_number"))
        if is_spare_detail(rec, cfg):
            # spares are told apart only by their way / circuit number
            tail = u"#" + num if num else u"#spare#{0}".format(rec["id"])
        elif to:
            tail = to
        elif num:
            tail = u"#" + num
        else:
            tail = u"#solo#{0}".format(rec["id"])
        gkey = (rec.get("view_id"), src, tail)
        if gkey not in groups:
            groups[gkey] = []
            order.append(gkey)
        groups[gkey].append(rec)

    by_from_to, by_from_num, spare_model = {}, {}, {}
    for circuit in circuits:
        src = rules.norm(circuit.get("source_panel") or _f(circuit, "panel"))
        if not src:
            continue
        for target in _circuit_targets(circuit):
            by_from_to.setdefault((src, target), []).append(circuit)
        num = rules.norm(_f(circuit, "circuit_number"))
        if num:
            by_from_num.setdefault((src, num), []).append(circuit)
        if circuit.get("is_spare") or circuit.get("is_space"):
            spare_model[src] = spare_model.get(src, 0) + 1

    matched_circuits, spare_sld, cross_view = set(), {}, {}
    for gkey in order:
        members = groups[gkey]
        _view, src, _to = gkey
        label = u"{0} → {1}".format(_f(members[0], "fed_from"),
                                         _f(members[0], "feeder") or
                                         (u"CCT " + _f(members[0], "circuit_number")
                                          if _f(members[0], "circuit_number") else u"?"))
        roles = {}
        for rec in members:
            roles.setdefault(rec.get("role"), []).append(rec)
        dup = [r for r in roles.values() if len(r) > 1]
        if dup:
            # a feeder drawn in several pieces (cable split across the sheet) is
            # fine when every piece carries the same data; different data means
            # different feeders sharing one name -> error, not compared
            clash = [recs for recs in dup if not _feeder_pieces_agree(recs)]
            for recs in dup:
                if recs in clash:
                    out.add(C_MATCH, ERROR, u"Duplicate Detail Item", label,
                            u"{0} {1} symbols for the same feeder in one view with different "
                            u"data - different feeders share this name? Not compared."
                            .format(len(recs), recs[0].get("role")),
                            u" | ".join(sorted(set(_f(r, "cable_desc") or _f(r, "breaker_ref")
                                                   or r.get("label") or u"" for r in recs))),
                            u"", _ids(recs), [], _views(recs))
                else:
                    out.add(C_MATCH, INFO, u"Feeder sections (SLD)", label,
                            u"{0} {1} symbols with identical data - one feeder drawn in pieces."
                            .format(len(recs), recs[0].get("role")),
                            u"", u"", _ids(recs), [], _views(recs))
            if clash:
                continue
        merged = _merge_group(out, members, label)
        spare = is_spare_detail({"f": merged}, cfg)
        if spare:
            spare_sld[src] = spare_sld.get(src, 0) + 1
        to, num = rules.norm(merged.get("feeder")), rules.norm(merged.get("circuit_number"))
        if spare:
            to = u""            # 'SPARE' in the feeder field is not a load name
        if not to and not num:
            if not spare:
                out.add(C_CIRCUIT, WARNING, u"Feeder without load", label,
                        u"Symbol has a source but no Feeder / circuit number - cannot match.",
                        u"", u"", _ids(members), [], _views(members))
            continue
        cross_view.setdefault((src, to or u"#" + num), []).extend(members)
        by_number = bool(num)
        candidates = by_from_num.get((src, num), []) if num else by_from_to.get((src, to), [])
        if num and not candidates and to and not spare:
            candidates, by_number = by_from_to.get((src, to), []), False
        stats["feeders_by_panel"].setdefault(_f(members[0], "fed_from"), 0)
        if not candidates:
            if src not in eq_index:
                msg = u"Source panel '{0}' is not in the model.".format(_f(members[0], "fed_from"))
            else:
                msg = u"No circuit from '{0}' to this load in the model.".format(
                    _f(members[0], "fed_from"))
                down = eq_index.get(to) or []
                if len(down) == 1 and _f(down[0], "fed_from"):
                    msg += u" Model supplies '{0}' from '{1}'.".format(
                        _f(down[0], "name"), _f(down[0], "fed_from"))
            out.add(C_MATCH, WARNING if spare else ERROR, u"Missing circuit in model", label,
                    msg, label, u"", _ids(members), [], _views(members))
            continue
        if len(candidates) > 1:
            out.add(C_MATCH, ERROR, u"Ambiguous match", label,
                    u"{0} model circuits match ({1}) - not compared.".format(
                        len(candidates), u", ".join(_f(c, "circuit_number") or u"?"
                                                    for c in candidates)),
                    label, u"", _ids(members), _ids(candidates), _views(members))
            continue
        circuit = candidates[0]
        matched_circuits.add(circuit["id"])
        stats["matched_feeders"] += 1
        out.passed(C_MATCH)
        if circuit.get("downstream"):
            stats["outgoing_feeders"] += 1
            stats["feeders_by_panel"][_f(members[0], "fed_from")] += 1
        _compare_feeder(out, cfg, label, merged, members, circuit, eq_index, by_number)

    for (src, to), recs in sorted(cross_view.items()):
        views = set(r.get("view_id") for r in recs)
        if len(views) > 1:
            out.add(C_MATCH, INFO if cfg["allow_same_name_in_different_views"] else ERROR,
                    u"Feeder drawn in several views",
                    u"{0} → {1}".format(_f(recs[0], "fed_from"), _f(recs[0], "feeder")),
                    u"Same feeder appears in {0} views: {1}.".format(len(views), _views(recs)),
                    u"", u"", _ids(recs), [], _views(recs))

    # ---- model circuits not on the SLD ------------------------------------------
    for circuit in circuits:
        if circuit["id"] in matched_circuits:
            continue
        src_text = circuit.get("source_panel") or _f(circuit, "panel")
        src = rules.norm(src_text)
        if not src or src not in drawn:
            continue
        if circuit.get("is_spare") or circuit.get("is_space"):
            continue
        if circuit.get("downstream"):
            out.add(C_MATCH, ERROR, u"Missing Detail Item (feeder)",
                    u"{0} → {1}".format(src_text, u", ".join(circuit["downstream"])),
                    u"Outgoing feeder (circuit {0}) is not drawn on the SLD.".format(
                        _f(circuit, "circuit_number")),
                    u"", _f(circuit, "load_name"), [], [circuit["id"]])
        elif cfg["expect_all_circuits_on_sld"]:
            out.add(C_MATCH, WARNING, u"Missing Detail Item (circuit)",
                    u"{0} → {1}".format(src_text, _f(circuit, "load_name")),
                    u"Circuit {0} is not drawn on the SLD.".format(_f(circuit, "circuit_number")),
                    u"", _f(circuit, "load_name"), [], [circuit["id"]])

    if cfg["report_spare_circuits_missing_on_sld"]:
        for src in sorted(set(spare_sld) | set(k for k in spare_model if k in drawn)):
            a, b = spare_sld.get(src, 0), spare_model.get(src, 0)
            if a == b:
                out.passed(C_BREAKER)
                continue
            name = src
            if eq_index.get(src):
                name = _f(eq_index[src][0], "name")
            out.add(C_BREAKER, WARNING, u"Spare count differs", name,
                    u"SLD shows {0} spare way(s), model has {1} spare/space circuit(s).".format(a, b),
                    rules.to_text(a), rules.to_text(b), [],
                    _ids(eq_index.get(src, [])))
    return out, stats
