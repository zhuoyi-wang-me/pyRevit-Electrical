# -*- coding: utf-8 -*-
"""
Electrical Calculator - calculation core.

Pure Python (no Revit / WPF imports) so it runs under IronPython 2.7, CPython 3
and the unit tests in tests/ElectricalCalculator.

Units: V, A, kW, kVA, kVAr, m, mm2, mV/A/m, kA.

Cable data = copper, steel-wire armoured multicore, Reference Method C (clipped
direct), 30 degC ambient - typical values from BS 7671 Appendix 4:
    PVC 70 degC  : Table 4D4A (current) / 4D4B (volt drop)
    XLPE 90 degC : Table 4E4A (current) / 4E4B (volt drop)
Always check against the edition / installation method used on your project.
"""
from __future__ import division

import math

SQRT3 = math.sqrt(3.0)

# Standard protective device ratings (A) - MCB / MCCB / ACB ranges.
BREAKER_RATINGS = [6, 10, 16, 20, 25, 32, 40, 50, 63, 80, 100, 125, 160, 200, 250,
                   315, 400, 500, 630, 800, 1000, 1250, 1600, 2000, 2500, 3200, 4000]

# size mm2 : (It 1-ph 2-core, It 3-ph 3/4-core, mV/A/m 1-ph, mV/A/m 3-ph)
CABLES = {
    'XLPE': {
        1.5: (27, 23, 31.0, 27.0),
        2.5: (36, 31, 19.0, 16.0),
        4: (49, 42, 12.0, 10.0),
        6: (62, 53, 7.9, 6.8),
        10: (85, 73, 4.7, 4.0),
        16: (110, 94, 2.9, 2.5),
        25: (146, 124, 1.85, 1.6),
        35: (180, 154, 1.35, 1.15),
        50: (219, 187, 0.98, 0.86),
        70: (279, 238, 0.67, 0.59),
        95: (338, 289, 0.49, 0.43),
        120: (392, 335, 0.39, 0.34),
        150: (451, 386, 0.32, 0.28),
        185: (515, 441, 0.26, 0.22),
        240: (607, 520, 0.21, 0.18),
        300: (698, 599, 0.17, 0.15),
    },
    'PVC': {
        1.5: (21, 18, 29.0, 25.0),
        2.5: (28, 25, 18.0, 15.0),
        4: (38, 33, 11.0, 9.5),
        6: (49, 42, 7.3, 6.4),
        10: (67, 58, 4.4, 3.8),
        16: (89, 77, 2.8, 2.4),
        25: (118, 102, 1.75, 1.5),
        35: (145, 125, 1.25, 1.1),
        50: (175, 151, 0.93, 0.81),
        70: (222, 192, 0.63, 0.55),
        95: (269, 231, 0.47, 0.41),
        120: (310, 267, 0.38, 0.33),
        150: (356, 306, 0.31, 0.27),
        185: (405, 348, 0.26, 0.22),
        240: (476, 409, 0.21, 0.18),
        300: (547, 469, 0.17, 0.15),
    },
}


class CalcError(ValueError):
    """Input problem the user can fix - shown in the window instead of a result."""


# ------------------------------------------------------------------ helpers
def _check_phase(phases):
    if phases not in (1, 3):
        raise CalcError('Phases must be 1 or 3.')


def _positive(value, label):
    if value is None or value <= 0:
        raise CalcError('{0} must be greater than 0.'.format(label))


def _pf(pf):
    if pf is None or pf <= 0 or pf > 1:
        raise CalcError('Power factor must be between 0 and 1.')


def _k(phases, volts):
    """Current-to-power factor: I = S / k   (k = V or sqrt(3)*V)."""
    return volts if phases == 1 else SQRT3 * volts


def cable_sizes(insulation):
    return sorted(CABLES[insulation])


def cable_data(insulation, size_mm2, phases):
    """(It tabulated A, mV/A/m) for a cable."""
    try:
        it1, it3, mv1, mv3 = CABLES[insulation][size_mm2]
    except KeyError:
        raise CalcError('No data for {0} {1} mm2.'.format(insulation, size_mm2))
    return (it1, mv1) if phases == 1 else (it3, mv3)


def next_breaker(current):
    for r in BREAKER_RATINGS:
        if r >= current:
            return r
    return None


# ------------------------------------------------------------ 1 load current
def load_current(phases, volts, value, unit, pf=1.0):
    """
    Convert between kW, kVA and A.
    unit: 'kW' | 'kVA' | 'A'. Returns dict amps, kw, kva, kvar.
    """
    _check_phase(phases)
    _positive(volts, 'Voltage')
    _positive(value, 'Value')
    _pf(pf)
    k = _k(phases, volts)
    if unit == 'kW':
        kva = value / pf
    elif unit == 'kVA':
        kva = value
    elif unit == 'A':
        kva = value * k / 1000.0
    else:
        raise CalcError('Unknown unit "{0}".'.format(unit))
    amps = kva * 1000.0 / k
    kw = kva * pf
    kvar = math.sqrt(max(kva * kva - kw * kw, 0.0))
    return {'amps': amps, 'kw': kw, 'kva': kva, 'kvar': kvar}


# ------------------------------------------------------------ 2 voltage drop
def voltage_drop(phases, volts, current, length_m, mv_per_a_m, limit_pct=None):
    """
    BS 7671 method: Vd = (mV/A/m) x Ib x L / 1000.
    The 3-phase mV/A/m tables already include sqrt(3) -> result is line-to-line.
    """
    _check_phase(phases)
    _positive(volts, 'Voltage')
    _positive(current, 'Current')
    _positive(length_m, 'Length')
    _positive(mv_per_a_m, 'mV/A/m')
    vd = mv_per_a_m * current * length_m / 1000.0
    pct = vd / volts * 100.0
    result = {'vd_v': vd, 'vd_pct': pct, 'v_end': volts - vd, 'ok': None,
              'max_length_m': None}
    if limit_pct:
        result['ok'] = pct <= limit_pct + 1e-9
        result['max_length_m'] = limit_pct / 100.0 * volts * 1000.0 / (mv_per_a_m * current)
    return result


# ------------------------------------------------------- 3 cable & breaker
def size_cable(phases, volts, ib, length_m, insulation='XLPE', ca=1.0, cg=1.0, ci=1.0,
               vd_limit_pct=5.0, breaker=None):
    """
    BS 7671 Appendix 4 sizing:  Ib <= In <= Iz,  It >= In / (Ca x Cg x Ci),
    then the smallest size whose volt drop is within the limit.
    breaker: fixed In (A) or None = next standard rating above Ib.
    """
    _check_phase(phases)
    _positive(volts, 'Voltage')
    _positive(ib, 'Design current Ib')
    _positive(length_m, 'Length')
    for f, label in ((ca, 'Ca'), (cg, 'Cg'), (ci, 'Ci')):
        if f is None or f <= 0 or f > 1.5:
            raise CalcError('Correction factor {0} must be between 0 and 1.5.'.format(label))
    if insulation not in CABLES:
        raise CalcError('Unknown insulation "{0}".'.format(insulation))

    if breaker:
        if breaker < ib:
            raise CalcError('Breaker In ({0:g} A) is below the design current Ib ({1:g} A).'.format(breaker, ib))
        in_a = breaker
    else:
        in_a = next_breaker(ib)
        if in_a is None:
            raise CalcError('Ib above {0} A - use parallel cables / busbar.'.format(BREAKER_RATINGS[-1]))

    factor = ca * cg * ci
    it_required = in_a / factor
    by_current = None
    chosen = None
    for size in cable_sizes(insulation):
        it, mv = cable_data(insulation, size, phases)
        if it < it_required:
            continue
        if by_current is None:
            by_current = size
        vd = voltage_drop(phases, volts, ib, length_m, mv, vd_limit_pct)
        if vd['ok'] is not False:
            chosen = {'size_mm2': size, 'it': it, 'iz': it * factor, 'mv_per_a_m': mv,
                      'vd_v': vd['vd_v'], 'vd_pct': vd['vd_pct']}
            break

    return {'in_a': in_a, 'factor': factor, 'it_required': it_required,
            'size_by_current': by_current, 'cable': chosen,
            'governed_by': None if chosen is None else
            ('voltage drop' if chosen['size_mm2'] != by_current else 'current')}


# ---------------------------------------------------------- 4 PF correction
def pf_correction(kw, pf_now, pf_target, phases=3, volts=400.0):
    """Capacitor kVAr = P (tan phi1 - tan phi2)."""
    _positive(kw, 'Active power')
    _pf(pf_now)
    _pf(pf_target)
    if pf_target <= pf_now:
        raise CalcError('Target power factor must be higher than the existing one.')
    _check_phase(phases)
    _positive(volts, 'Voltage')
    tan1 = math.tan(math.acos(pf_now))
    tan2 = math.tan(math.acos(pf_target))
    kva1, kva2 = kw / pf_now, kw / pf_target
    k = _k(phases, volts)
    return {'kvar': kw * (tan1 - tan2), 'kva_before': kva1, 'kva_after': kva2,
            'amps_before': kva1 * 1000.0 / k, 'amps_after': kva2 * 1000.0 / k,
            'kva_released': kva1 - kva2}


# ------------------------------------------------ 0 loads from the model
def _most_common(values):
    counts = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    return max(sorted(counts), key=lambda v: counts[v]) if counts else None


UNCLASSIFIED = u'(no load classification)'
PANEL_DEMAND = u'(panels - own demand load)'


def _factor(f):
    """Revit stores factors as fractions (1.0 = 100 %); accept percentages too."""
    f = float(f)
    return f / 100.0 if f > 2.0 else f


def _factor_for(x, values):
    """Factor of the range containing x (ranges: [(min, max, factor)])."""
    vs = sorted(values)
    for lo, hi, f in vs:
        if lo <= x <= hi:
            return _factor(f)
    if x > vs[-1][1]:
        return _factor(vs[-1][2])
    return _factor(vs[0][2])


def apply_demand(loads_va, rule, values, additional_va=0.0):
    """
    Demand load (VA) of one load classification - same rule types as Revit's
    Electrical Demand Factors (Manage > MEP Settings > Demand Factors):

    Constant                : every load x factor
    QuantityTable           : total x factor of the range containing the element COUNT
    QuantityTablePerPortion : n-th element x factor of the range containing n
                              (largest loads counted first - conservative)
    LoadTable               : total x factor of the range containing the TOTAL load
    LoadTablePerPortion     : each slice of the total x its range factor
                              (e.g. first 10 kVA at 100 %, remainder at 50 %)
    values : [(min, max, factor)] - min/max in VA (load rules) or count (quantity rules)
    """
    loads = [float(v) for v in loads_va if v]
    total = sum(loads)
    if not loads:
        return 0.0
    rule = str(rule or 'Constant')
    values = [(float(lo), float(hi), f) for lo, hi, f in (values or [])]
    if not values:
        demand = total
    elif rule.endswith('QuantityTablePerPortion'):
        demand = sum(va * _factor_for(n + 1, values)
                     for n, va in enumerate(sorted(loads, reverse=True)))
    elif rule.endswith('QuantityTable'):
        demand = total * _factor_for(len(loads), values)
    elif rule.endswith('LoadTablePerPortion'):
        vs = sorted(values)
        demand = sum(max(0.0, min(total, hi) - lo) * _factor(f) for lo, hi, f in vs)
        if total > vs[-1][1]:
            demand += (total - vs[-1][1]) * _factor(vs[-1][2])
    elif rule.endswith('LoadTable'):
        demand = total * _factor_for(total, values)
    else:                                                  # Constant
        demand = total * _factor(values[0][2])
    return demand + (additional_va or 0.0)


def describe_rule(definition):
    rule = str(definition.get('rule') or 'Constant').split('.')[-1]
    values = definition.get('values') or []
    if rule == 'Constant' and values:
        text = u'constant {0:g} %'.format(_factor(values[0][2]) * 100)
    else:
        text = u'{0}, {1} range(s)'.format(rule, len(values))
    if definition.get('additional_va'):
        text += u' + {0:,.0f} VA'.format(definition['additional_va'])
    return text


def summarise_loads(items, supply='auto', volts=None, demand_pct=100.0, default_pf=0.9, demand_defs=None):
    """
    Total the loads of selected Revit elements.

    items       : [{'va', 'volts', 'poles', 'pf', 'category', 'classification', 'demand_va'}]
                  values may be None / 0; 'demand_va' is set for panels only
    supply      : 'auto' (3-phase if any 2/3-pole load, else 1-phase) | 1 | 3
    volts       : supply voltage; None = from the elements (most common), else 230 / 400
    demand_pct  : manual mode (demand_defs None): factor for everything;
                  classification mode: fallback for loads without a demand factor
    default_pf  : used for elements without a valid power factor
    demand_defs : None = manual mode, else {classification name: {'name', 'rule',
                  'values': [(min, max, factor)], 'additional_va'}} - the project's
                  demand factor per load classification (missing key = no demand factor)
    """
    _pf(default_pf)
    if demand_pct is None or demand_pct <= 0 or demand_pct > 100:
        raise CalcError('Demand factor must be between 1 and 100 %.')
    if not items:
        raise CalcError('Nothing selected - select electrical fixtures / equipment in Revit first.')
    loaded = [i for i in items if (i.get('va') or 0) > 0]
    if not loaded:
        raise CalcError('None of the {0} selected element(s) has an Apparent Load.'.format(len(items)))

    multi = [i for i in loaded if (i.get('poles') or 1) >= 2]
    if supply == 'auto':
        phases = 3 if multi else 1
    elif supply in (1, 3):
        phases = supply
    else:
        raise CalcError('Supply must be auto, 1 or 3.')

    warnings = []
    if volts is None:
        pool = multi if phases == 3 and multi else ([] if phases == 3 else loaded)
        volts = _most_common([round(i['volts']) for i in pool if (i.get('volts') or 0) > 0])
        if not volts or (phases == 3 and volts < 300):
            volts = 400.0 if phases == 3 else 230.0
    _positive(volts, 'Voltage')
    if phases == 1 and multi:
        warnings.append('{0} multi-pole load(s) on a 1-phase supply.'.format(len(multi)))
    item_volts = set(round(i['volts']) for i in loaded if (i.get('volts') or 0) > 0)
    if len(item_volts) > 1:
        warnings.append('Mixed voltages in selection: {0} V.'.format(
            ', '.join('{0:g}'.format(v) for v in sorted(item_volts))))
    no_pf = 0
    kw = 0.0
    by_cat = {}
    groups = {}                                            # classification -> [items]
    for i in loaded:
        pf = i.get('pf')
        if not pf or pf <= 0 or pf > 1:
            pf = default_pf
            no_pf += 1
        i_kw = i['va'] * pf / 1000.0
        kw += i_kw
        cat = i.get('category') or 'Other'
        n, va = by_cat.get(cat, (0, 0.0))
        by_cat[cat] = (n + 1, va + i['va'])
        key = UNCLASSIFIED if demand_defs is None else (i.get('classification') or UNCLASSIFIED)
        if i.get('demand_va') is not None and demand_defs is not None:
            key = PANEL_DEMAND
        groups.setdefault(key, []).append((i, i_kw))

    # ---- demand per load classification (Revit applies demand factors per classification)
    fallback = demand_pct / 100.0
    by_class = []
    demand_va_total = demand_kw_total = 0.0
    for name in sorted(groups):
        members = groups[name]
        conn_va = sum(i['va'] for i, _ in members)
        conn_kw = sum(k for _, k in members)
        definition = (demand_defs or {}).get(name) if name not in (UNCLASSIFIED, PANEL_DEMAND) else None
        if name == PANEL_DEMAND:
            dem_va = sum(i['demand_va'] if i['demand_va'] > 0 else i['va'] for i, _ in members)
            rule = u'panel Total Demand Load'
        elif definition:
            dem_va = apply_demand([i['va'] for i, _ in members], definition.get('rule'),
                                  definition.get('values'), definition.get('additional_va') or 0.0)
            rule = u'{0} ({1})'.format(definition.get('name') or '-', describe_rule(definition))
        else:
            dem_va = conn_va * fallback
            rule = (u'manual {0:g} %'.format(demand_pct) if demand_defs is None
                    else u'no demand factor - fallback {0:g} %'.format(demand_pct))
        ratio = dem_va / conn_va if conn_va else 1.0
        demand_va_total += dem_va
        demand_kw_total += conn_kw * ratio
        by_class.append({'name': name, 'count': len(members), 'connected_va': conn_va,
                         'demand_va': dem_va, 'factor': ratio, 'rule': rule})

    kva = sum(i['va'] for i in loaded) / 1000.0
    demand_kva = demand_va_total / 1000.0
    ib = demand_kva * 1000.0 / _k(phases, volts)
    if phases == 3 and len(multi) < len(loaded):
        warnings.append('Single-phase loads assumed balanced across L1/L2/L3.')
    if demand_defs is not None and UNCLASSIFIED in groups:
        warnings.append('{0} element(s) without a load classification use the fallback {1:g} %.'.format(
            len(groups[UNCLASSIFIED]), demand_pct))
    return {'count': len(items), 'with_load': len(loaded), 'missing': len(items) - len(loaded),
            'no_pf': no_pf, 'phases': phases, 'volts': float(volts),
            'connected_kva': kva, 'connected_kw': kw, 'pf': kw / kva,
            'demand_kva': demand_kva, 'demand_kw': demand_kw_total, 'demand_factor': demand_kva / kva,
            'ib': ib, 'breaker': next_breaker(ib), 'by_category': by_cat,
            'by_classification': by_class, 'mode': 'manual' if demand_defs is None else 'classification',
            'warnings': warnings}


# ---------------------------------------------------------------- 6 isolators
# Standard switch-disconnector (isolator) ratings, IEC 60947-3 rotary / enclosed.
ISOLATOR_RATINGS = [16, 20, 25, 32, 40, 63, 80, 100, 125, 160, 200, 250, 315, 400,
                    500, 630, 800, 1000, 1250, 1600]

_MOTOR_WORDS = ('motor', 'hvac', 'mechanical', 'pump', 'fan', 'chiller', 'ahu', 'fcu', 'compressor',
                'lift', 'elevator', 'escalator', 'condens', 'cooling tower', 'ventilat', 'extract')


def is_motor_load(category=None, classification=None, name=None):
    """Motor-type load -> isolator needs AC-23A duty and a starting margin."""
    if (category or '').strip().lower() == 'mechanical equipment':
        return True
    text = u' '.join([classification or '', name or '']).lower()
    return any(w in text for w in _MOTOR_WORDS)


def isolator_rating(va, volts=None, poles=1, motor=False, motor_margin_pct=125.0,
                    other_margin_pct=100.0, min_rating=20, four_pole=False):
    """
    Isolator for one load.
        Ib       = VA / V          (1- or 2-pole)   |  VA / (sqrt3 x V)  (3-pole)
        required = Ib x margin     (motor 125 % default - starting current / AC-23A)
        rating   = next standard isolator >= max(required, min_rating)
        poles    : 1-pole load -> DP (L+N), 2-pole -> DP, 3-pole -> TP (or TP+N if four_pole)
        duty     : AC-23A motor loads, AC-22A others (IEC 60947-3)
    """
    _positive(va, 'Load (VA)')
    poles = int(poles or 1)
    if poles not in (1, 2, 3):
        raise CalcError('Poles must be 1, 2 or 3.')
    if not volts or volts <= 0:
        volts = 230.0 if poles == 1 else 400.0
    margin = (motor_margin_pct if motor else other_margin_pct) / 100.0
    if margin < 1.0:
        raise CalcError('Margin must be at least 100 %.')
    ib = va / (SQRT3 * volts) if poles == 3 else va / volts
    required = max(ib * margin, float(min_rating or 0))
    rating = None
    for r in ISOLATOR_RATINGS:
        if r >= required - 1e-9:
            rating = r
            break
    if poles == 3:
        pole_label = 'TP+N (4P)' if four_pole else 'TP (3P)'
    else:
        pole_label = 'DP (2P)'
    return {'ib': ib, 'required': ib * margin, 'rating': rating, 'poles': pole_label,
            'phases': 3 if poles == 3 else 1, 'volts': float(volts),
            'duty': 'AC-23A' if motor else 'AC-22A', 'motor': bool(motor),
            'note': '' if rating else 'above {0} A - use a switch-fuse / ACB'.format(ISOLATOR_RATINGS[-1])}


# ----------------------------------------------------------- 5 short circuit
def transformer_fault(kva, volts, z_pct, source_mva=None):
    """
    Symmetrical 3-phase fault at the transformer LV terminals.
    Per unit on the transformer base: Isc = Ifl / (z_tx + z_source),
    z_source = kVA / (1000 x source MVA); source_mva None/0 = infinite bus.
    """
    _positive(kva, 'Transformer rating')
    _positive(volts, 'Secondary voltage')
    _positive(z_pct, 'Impedance %Z')
    ifl = kva * 1000.0 / (SQRT3 * volts)
    z_tx = z_pct / 100.0
    z_src = kva / (1000.0 * source_mva) if source_mva else 0.0
    isc = ifl / (z_tx + z_src)
    return {'ifl_a': ifl, 'isc_ka': isc / 1000.0,
            'fault_mva': SQRT3 * volts * isc / 1e6,
            'isc_inf_ka': ifl / z_tx / 1000.0}
