# -*- coding: utf-8 -*-
"""Parameter mapping, check selection and templates for the SLD Checker.

HOW TO CONFIGURE
  Click "SLD Checker": the setup window maps every field to a parameter
  (drop-downs list the parameters really found in the model), switches
  checks on/off and saves it all as a named TEMPLATE in
  %APPDATA%\\EleTools\\SLDChecker\\templates\\<name>.json (Export / Import
  to share templates with the team). Anything missing in a template falls
  back to the DEFAULTS below.

PARAMETER LISTS
  Every field is a list of sources tried IN ORDER; the first one that
  exists AND has a value wins (instance first, then type).
    "MEE-FedFrom"                  -> parameter by name (shared / project /
                                      family parameter)
    "bip:RBS_ELEC_PANEL_NAME"      -> Revit built-in parameter
  Name matching ignores case, spaces, '-' and '_' in the PARAMETER NAME, so
  "MEE-Component" also finds "MEE_Component".

  !! The SLD (detail item) names below are best guesses based on the other
  !! Single Line tools (MEE-Component, MEE-FedFrom, MEE-Feeder, MEE-CBRef,
  !! MEE-CableRef). Check them against your detail item families once.
"""
import copy
import io
import json
import os

CONFIG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")),
                          "EleTools", "SLDChecker")
CONFIG_PATH = os.path.join(CONFIG_DIR, "sld_checker_config.json")

DEFAULTS = {
    "schema_version": 1,

    # ------------------------------------------------------------------
    # Detail Items on the SLD (category Detail Items)
    # ------------------------------------------------------------------
    "sld_fields": {
        # PRIMARY KEY of a board / equipment symbol
        "name": ["MEE-Component", "MEE-EquipmentTag", "Panel Name", "Equipment Name"],
        "equipment_name": ["MEE-EquipmentName", "MEE-Description"],
        "classification": ["MEE-EquipmentType", "MEE-Classification", "Equipment Type"],
        # upstream board this symbol / feeder is fed from
        "fed_from": ["MEE-FedFrom", "Fed From", "Supply From"],
        # downstream board / load a breaker or cable feeds
        "feeder": ["MEE-Feeder", "Feeder", "Feeds"],
        "circuit_number": ["MEE-CircuitNo", "MEE-CircuitNumber", "Circuit Number", "CKT NO"],
        "breaker_ref": ["MEE-CBRef"],
        "breaker_rating": ["MEE-BreakerRating", "MEE-CBRating", "Breaker Rating",
                           "BCC_Breaker Rating", "Rating"],
        "breaker_frame": ["MEE-FrameRating", "Frame Rating", "Frame"],
        "poles": ["MEE-Poles", "Number of Poles", "No. of Poles", "Poles"],
        "spare": ["MEE-Spare", "Spare"],
        "cable_type": ["MEE-CableType", "Cable Type"],
        "cable_size": ["MEE-CableSize", "Cable Size", "BCC_Cable"],
        "cores": ["MEE-Cores", "Number of Cores", "No. of Cores", "Cores"],
        "cable_desc": ["MEE-CableRef", "MEE-CableDescription", "Cable Description"],
        "connected_load": ["MEE-TotalConnectedLoad", "Total Connected Load", "Connected Load"],
        "demand_load": ["MEE-MaximumDemand", "MEE-MaxDemand", "Maximum Demand", "Demand Load"],
    },

    # How a detail item is recognised as a board, a breaker or a cable.
    # Keywords are searched in "Family : Type" (case-insensitive), first
    # role that matches wins. Items matching nothing are classified by the
    # fields they carry (cable fields -> cable, breaker fields -> breaker,
    # name -> equipment); items with none are ignored and only counted.
    "sld_roles": [
        # first: annotation-like families that are never boards / feeders
        ["ignore", ["paneldetail", "panel detail", "interlock", "legend", "title block",
                    "keyplan", "key plan"]],
        ["cable", ["cable", "feeder cable", "wire"]],
        ["breaker", ["breaker", "mccb", "mcb", "acb", "rcbo", "rccb", "elcb",
                     "isolator", "fuse", "switch disconnector", "cb"]],
        ["equipment", ["panel", "board", "mdb", "smdb", "db", "transformer", "tx",
                       "generator", "ups", "mcc", "switchgear"]],
    ],
    # Text in the feeder / name / spare field that marks a spare way
    "spare_keywords": ["spare", "space"],
    # SLD board names with no exact model match are tried again without these
    # endings (regex), e.g. 'PP-ST-SE-07-02-09L(E)' -> 'PP-ST-SE-07-02-09L'.
    # A match found this way is reported as a WARNING, never silently.
    "name_suffix_patterns": ["\\s*\\([A-Za-z0-9]{1,3}\\)$"],

    # ------------------------------------------------------------------
    # Model elements
    # ------------------------------------------------------------------
    "model_equipment_fields": {
        "name": ["bip:RBS_ELEC_PANEL_NAME"],
        "equipment_name": ["MEE-EquipmentName", "MEE-Description"],
        "classification": ["MEE-EquipmentType", "MEE-Classification"],
        "fed_from": ["bip:RBS_ELEC_PANEL_SUPPLY_FROM_PARAM", "MEE-FedFrom"],
        "connected_load": ["bip:RBS_ELEC_PANEL_TOTALLOAD_PARAM", "Total Connected Load"],
        "demand_load": ["bip:RBS_ELEC_PANEL_TOTALESTLOAD_PARAM", "Total Estimated Demand",
                        "Total Demand Load", "MEE-MaximumDemand"],
    },
    "model_circuit_fields": {
        "circuit_number": ["bip:RBS_ELEC_CIRCUIT_NUMBER"],
        "load_name": ["bip:RBS_ELEC_CIRCUIT_NAME"],
        "panel": ["bip:RBS_ELEC_CIRCUIT_PANEL_PARAM"],
        "breaker_rating": ["bip:RBS_ELEC_CIRCUIT_RATING_PARAM"],
        "breaker_frame": ["bip:RBS_ELEC_CIRCUIT_FRAME_PARAM"],
        "poles": ["bip:RBS_ELEC_NUMBER_OF_POLES"],
        "cable_type": ["MEE-CableType", "bip:RBS_ELEC_CIRCUIT_WIRE_TYPE_PARAM"],
        "cable_size": ["MEE-CableSize", "BCC_Cable", "bip:RBS_ELEC_CIRCUIT_WIRE_SIZE_PARAM"],
        "cores": ["MEE-Cores"],
        "cable_desc": ["MEE-CableRef", "MEE-CableDescription"],
        "connected_load": ["bip:RBS_ELEC_APPARENT_LOAD"],
        "connected_load_kw": ["bip:RBS_ELEC_TRUE_LOAD"],
        "demand_load": ["MEE-MaximumDemand"],
        "design_current": ["bip:RBS_ELEC_APPARENT_CURRENT_PARAM"],
    },
    # Electrical Fixtures only join the matching when they carry a name here
    "model_fixture_fields": {
        "name": ["MEE-Component", "MEE-EquipmentTag"],
        "fed_from": ["bip:RBS_ELEC_CIRCUIT_PANEL_PARAM", "MEE-FedFrom"],
        "connected_load": ["bip:RBS_ELEC_APPARENT_LOAD"],
    },

    # Unit of each numeric field. Revit internal values are converted to it;
    # SLD text like "63A", "250 kVA", "1.2 MVA", "10 kW" is parsed.
    "units": {
        "breaker_rating": "A", "breaker_frame": "A", "design_current": "A",
        "connected_load": "kVA", "demand_load": "kVA", "connected_load_kw": "kW",
    },
    # Load text on the SLD without a unit is read as this
    "default_load_unit": "kVA",

    # ------------------------------------------------------------------
    # Classification of boards (first matching rule wins). Regex on the
    # normalised name, then on "Family : Type", then on the family Part Type.
    # ------------------------------------------------------------------
    "classification_rules": [
        ["TX", "transformer|^(tx|tr|trf|xfmr)[\\s\\-_\\.]*\\d"],
        ["SMDB", "smdb|sub[\\s\\-_]*main"],
        ["MDB", "(^|[^s])mdb|main[\\s\\-_]*distribution|^msb|main[\\s\\-_]*switch[\\s\\-_]*board|switchboard"],
        ["DB", "db|distribution[\\s\\-_]*board|panelboard|panel"],
    ],

    # ------------------------------------------------------------------
    # Tolerances and switches
    # ------------------------------------------------------------------
    "load_tolerance_pct": 2.0,          # loads within 2 % are a match
    "rating_tolerance_a": 0.5,          # breaker ratings within 0.5 A
    "cable_size_tolerance_mm2": 0.01,
    # The same board drawn on an overview sheet and a detail sheet is fine;
    # the same name twice in ONE view is reported as a duplicate.
    "allow_same_name_in_different_views": True,
    # Normally only circuits feeding a board (outgoing feeders) are expected
    # on the SLD. True = also expect every final circuit.
    "expect_all_circuits_on_sld": False,
    "report_spare_circuits_missing_on_sld": True,
    # A board built from several sections (same Panel Name, same family/type)
    # is one board: model sections must lie within this distance of each other;
    # SLD sections must be in the same view and within sld_section_distance_mm on
    # paper. Anything else stays a duplicate. False = always report duplicates.
    "merge_board_sections": True,
    "section_merge_distance_m": 10.0,
    "sld_section_distance_mm": 60.0,     # on paper, using the view scale
    # Equipment / circuits in loaded Revit links join the matching (multi-model
    # projects). Linked boards not drawn are only reported when their source is drawn.
    "include_linked_models": True,
    # Max rows per section in the Revit output window (CSV always has all)
    "max_rows_per_section": 400,

    # Which checks to report: {check key: false} switches one off (see
    # checks.CHECK_ITEMS). Missing keys = on.
    "checks": {},
    "show_info": True,
    # "all" | "active" | "pick" - remembered with the template
    "scope_mode": "all",
}

# ----------------------------------------------------------------------
# Labels for the setup window + keywords for "Auto-detect"
# (fragments of the parameter name after removing spaces, '-' and '_')
# ----------------------------------------------------------------------
FIELD_INFO = [
    # field, label, hint, auto-detect fragments (most specific first)
    ("name", u"Panel / equipment name", u"Primary key - must equal the model Panel Name",
     ["component", "panelname", "equipmenttag", "boardname", "dbname"]),
    ("equipment_name", u"Equipment description", u"Optional second name / description",
     ["equipmentname", "description"]),
    ("classification", u"Classification", u"TX / MDB / SMDB / DB text",
     ["equipmenttype", "classification", "boardtype", "paneltype"]),
    ("fed_from", u"Fed from (source panel)", u"Upstream board name", ["fedfrom", "supplyfrom", "source"]),
    ("feeder", u"Feeds (downstream panel / load)", u"On breaker and cable symbols",
     ["feeder", "feeds", "toserve", "downstream", "loadname"]),
    ("circuit_number", u"Circuit number", u"Way / circuit no.", ["circuitno", "circuitnumber", "cktno", "wayno"]),
    ("breaker_ref", u"Breaker reference", u"Shown only (e.g. CB-01)", ["cbref", "breakerref"]),
    ("breaker_rating", u"Breaker rating (A)", u"e.g. 63A", ["breakerrating", "cbrating", "tripRating", "rating"]),
    ("breaker_frame", u"Breaker frame (A)", u"e.g. 250AF", ["framerating", "frame"]),
    ("poles", u"Number of poles", u"SP / DP / TP / TPN / 4P / 1..4", ["poles", "noofpoles"]),
    ("spare", u"Spare flag", u"Yes / SPARE marks a spare way", ["spare"]),
    ("cable_type", u"Cable type", u"e.g. XLPE/SWA/PVC", ["cabletype", "insulation"]),
    ("cable_size", u"Cable size (mm²)", u"e.g. 4C x 16mm²", ["cablesize", "conductorsize", "wiresize"]),
    ("cores", u"Number of cores", u"e.g. 4", ["cores", "noofcores", "core"]),
    ("cable_desc", u"Cable description", u"Free text / cable ref", ["cableref", "cabledescription", "cable"]),
    ("connected_load", u"Total connected load", u"kVA (or kW)", ["totalconnectedload", "connectedload", "tcl"]),
    ("demand_load", u"Maximum demand", u"kVA (or kW)", ["maximumdemand", "maxdemand", "demandload", "md"]),
]
FIELD_LABELS = dict((f[0], f[1]) for f in FIELD_INFO)
FIELD_LABELS.update({"load_name": u"Load name", "panel": u"Panel (source)",
                     "connected_load_kw": u"True load (kW)",
                     "design_current": u"Design current (A)"})

MODEL_SECTIONS = [
    ("model_equipment_fields", u"Electrical Equipment (panels, boards, transformers)"),
    ("model_circuit_fields", u"Electrical Circuits"),
    ("model_fixture_fields", u"Electrical Fixtures (only those with a name join the matching)"),
]


def names_to_text(names):
    return u", ".join(names or [])


def text_to_names(text):
    out = []
    for part in (text or u"").split(u","):
        part = part.strip()
        if part and part not in out:
            out.append(part)
    return out


def rules_to_text(rules_list):
    return u"\r\n".join(u"{0} = {1}".format(label, pattern) for label, pattern in rules_list)


def text_to_rules(text):
    """'LABEL = regex' lines -> [[label, regex]]; raises ValueError on a bad line."""
    import re
    out = []
    for number, line in enumerate((text or u"").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith(u"#"):
            continue
        if u"=" not in line:
            raise ValueError(u"Classification rule line {0} needs 'LABEL = regex'.".format(number))
        label, pattern = line.split(u"=", 1)
        label, pattern = label.strip().upper(), pattern.strip()
        if not label or not pattern:
            raise ValueError(u"Classification rule line {0} is incomplete.".format(number))
        try:
            re.compile(pattern)
        except re.error as error:
            raise ValueError(u"Classification rule '{0}': bad regex ({1}).".format(label, error))
        out.append([label, pattern])
    return out


def text_to_patterns(text):
    """One regex per line -> list; raises ValueError on a bad pattern."""
    import re
    out = []
    for line in (text or u"").splitlines():
        line = line.strip()
        if not line or line.startswith(u"#"):
            continue
        try:
            re.compile(line)
        except re.error as error:
            raise ValueError(u"Name ending pattern '{0}': bad regex ({1}).".format(line, error))
        out.append(line)
    return out


def auto_detect(field, available_names):
    """Best parameter name for a field among available_names, or None."""
    from sldcheck import rules
    fragments = []
    for info in FIELD_INFO:
        if info[0] == field:
            fragments = [f.lower() for f in info[3]]
    keys = [(rules.param_key(n), n) for n in available_names]
    for fragment in fragments:
        exact = [n for k, n in keys if k == fragment or k.endswith(fragment) and k.startswith("mee")]
        if exact:
            return sorted(exact)[0]
        partial = [n for k, n in keys if fragment in k]
        if len(partial) == 1:
            return partial[0]
    return None


# ----------------------------------------------------------------------
# Templates: one JSON per template in %APPDATA%\EleTools\SLDChecker\templates
# ----------------------------------------------------------------------
TEMPLATE_DIR = os.path.join(CONFIG_DIR, "templates")
SETTINGS_PATH = os.path.join(CONFIG_DIR, "settings.json")
LOG_PATH = os.path.join(CONFIG_DIR, "last_run.txt")


def log_path():
    """Folder is created on demand; the run log is rewritten every run."""
    if not os.path.isdir(CONFIG_DIR):
        os.makedirs(CONFIG_DIR)
    return LOG_PATH
DEFAULT_TEMPLATE = u"<Default>"
_BAD_CHARS = u'<>:"/\\|?*'


class TemplateError(Exception):
    pass


def _ensure_dirs():
    if not os.path.isdir(TEMPLATE_DIR):
        os.makedirs(TEMPLATE_DIR)


def _read_json(path):
    with io.open(path, "r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def _write_json(path, data):
    text = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False)
    if not isinstance(text, type(u"")):
        text = text.decode("utf-8")
    with io.open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def complete(data):
    """Defaults deep-merged with a (possibly older / partial) template."""
    return _merge(copy.deepcopy(DEFAULTS), data or {})


def clean_name(name):
    name = (name or u"").strip()
    if not name or name == DEFAULT_TEMPLATE or any(c in _BAD_CHARS for c in name):
        raise TemplateError(u"Template name must not be empty, '{0}' or contain {1}"
                            .format(DEFAULT_TEMPLATE, _BAD_CHARS))
    return name


def template_path(name):
    return os.path.join(TEMPLATE_DIR, clean_name(name) + u".json")


def list_templates():
    names = []
    if os.path.isdir(TEMPLATE_DIR):
        for entry in os.listdir(TEMPLATE_DIR):
            if entry.lower().endswith(".json"):
                names.append(entry[:-5])
    return [DEFAULT_TEMPLATE] + sorted(names, key=lambda n: n.lower())


def load_template(name):
    if not name or name == DEFAULT_TEMPLATE:
        return copy.deepcopy(DEFAULTS)
    try:
        return complete(_read_json(template_path(name)))
    except TemplateError:
        raise
    except Exception as error:
        raise TemplateError(u"Template '{0}' cannot be read: {1}".format(name, error))


def save_template(name, cfg):
    _ensure_dirs()
    data = copy.deepcopy(cfg)
    data["_template"] = clean_name(name)
    _write_json(template_path(name), data)
    set_last_template(name)
    return name


def delete_template(name):
    path = template_path(name)
    if os.path.exists(path):
        os.remove(path)


def export_template(cfg, path):
    _write_json(path, cfg)


def import_template(path):
    try:
        data = _read_json(path)
    except Exception as error:
        raise TemplateError(u"Cannot read {0}: {1}".format(path, error))
    if not isinstance(data, dict) or "sld_fields" not in data:
        raise TemplateError(u"This file is not an SLD Checker template:\n{0}".format(path))
    return complete(data)


def _settings():
    try:
        return _read_json(SETTINGS_PATH)
    except Exception:
        return {}


def get_last_template():
    name = _settings().get("last_template") or DEFAULT_TEMPLATE
    return name if name in list_templates() else DEFAULT_TEMPLATE


def set_last_template(name):
    try:
        if not os.path.isdir(CONFIG_DIR):
            os.makedirs(CONFIG_DIR)
        data = _settings()
        data["last_template"] = name
        _write_json(SETTINGS_PATH, data)
    except Exception:
        pass


def migrate_legacy():
    """The first version kept one JSON (sld_checker_config.json); turn it
    into the template 'My mapping' once."""
    if not os.path.exists(CONFIG_PATH):
        return
    target = os.path.join(TEMPLATE_DIR, u"My mapping.json")
    if os.path.exists(target) or _settings().get("legacy_migrated"):
        return
    try:
        _ensure_dirs()
        save_template(u"My mapping", complete(_read_json(CONFIG_PATH)))
        data = _settings()
        data["legacy_migrated"] = True
        _write_json(SETTINGS_PATH, data)
    except Exception:
        pass


def _merge(base, override):
    for key, value in (override or {}).items():
        if key.startswith("_"):
            continue
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value
    return base


def load_config():
    """Defaults deep-merged with the user's JSON. Returns (cfg, message)."""
    cfg = copy.deepcopy(DEFAULTS)
    if not os.path.exists(CONFIG_PATH):
        return cfg, u"Default mapping (no user config file)."
    try:
        with open(CONFIG_PATH, "r") as handle:
            user = json.load(handle)
        _merge(cfg, user)
        return cfg, u"Mapping from {0}".format(CONFIG_PATH)
    except Exception as error:
        return cfg, u"Could not read {0} ({1}) - using defaults.".format(CONFIG_PATH, error)


def write_user_config(overwrite=False):
    """Create the editable JSON copy of the defaults. Returns its path."""
    if not os.path.isdir(CONFIG_DIR):
        os.makedirs(CONFIG_DIR)
    if overwrite or not os.path.exists(CONFIG_PATH):
        data = copy.deepcopy(DEFAULTS)
        data["_readme"] = ("Edit parameter lists (tried in order; 'bip:NAME' = Revit "
                           "built-in parameter). Delete this file to restore defaults.")
        with open(CONFIG_PATH, "w") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
    return CONFIG_PATH
