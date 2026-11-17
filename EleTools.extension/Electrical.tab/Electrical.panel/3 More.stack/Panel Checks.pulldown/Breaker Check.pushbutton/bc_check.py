# -*- coding: utf-8 -*-
"""Breaker Check - the comparison. No Revit in this file.

current from the load:   1-pole and 2-pole   I = P / (V x PF)
                         3-pole              I = P / (1.73 x V x PF)
        P = load of the circuit in watts, V = voltage of the circuit in Revit (230 V / 400 V),
        PF = the power factor typed in the window (0.95 unless changed)
required breaker:        the rating the user set for the load type of the circuit (Rule);
                         when I x margin (1.25 unless changed) is more than that, or there is
                         no rule, the next rating of the user's list above I x margin
result:  UNDERSIZED when I x MARGIN is above the breaker rating
         OVERSIZED  when the breaker is larger than the next rating of the user's list above I x MARGIN
         otherwise OK
"""
import math
import re

OK, UNDERSIZED, NO_RATING, NO_LOAD, NO_VOLTAGE = 'OK', 'UNDERSIZED', 'NO RATING', 'NO LOAD', 'NO VOLTAGE'
CANNOT_READ = 'CANNOT READ'
OVERSIZED = 'OVERSIZED'                  # the breaker is larger than the next rating above the load current
MISMATCH = (UNDERSIZED, OVERSIZED, NO_RATING)
ORDER = [UNDERSIZED, OVERSIZED, NO_RATING, CANNOT_READ, NO_VOLTAGE, NO_LOAD, OK]   # how the result lines are sorted


ROOT3 = 1.73                             # as in the office calculation sheet


def load_current(load_w, voltage_v, poles, pf=1.0):
    """Current in amperes from the load in watts, or None when the voltage or the power factor is missing."""
    if not voltage_v or voltage_v <= 0 or not pf or pf <= 0:
        return None
    if poles == 3:
        return load_w / (ROOT3 * voltage_v * pf)
    return load_w / (voltage_v * pf)


def judge(current_a, rating_a, margin=1.0):
    """(result, percent of the breaker used or None)"""
    if current_a is None:
        return NO_VOLTAGE, None
    if not rating_a or rating_a <= 0:
        return NO_RATING, None
    used = current_a / rating_a * 100.0
    if current_a <= 1e-9:
        return NO_LOAD, 0.0
    if current_a * margin > rating_a + 1e-9:
        return UNDERSIZED, used
    return OK, used


def parse_ratings(text):
    """'16, 20; 25 32' -> [16.0, 20.0, 25.0, 32.0]. Raises ValueError for text that is not a number."""
    for separator in u';/|\t\r\n':
        text = (text or u'').replace(separator, u',')
    values = []
    for part in text.replace(u' ', u',').split(u','):
        if part.strip():
            value = float(part)
            if value <= 0:
                raise ValueError(part)
            values.append(value)
    return sorted(set(values))


def propose(current_a, margin, ratings):
    """The smallest rating of the user's list that is enough for the current, or None."""
    if current_a is None:
        return None
    fitting = [rating for rating in ratings if rating + 1e-9 >= current_a * margin]
    return min(fitting) if fitting else None


def parse_number(text):
    """Number typed by the user ('32', '32,5', '32 A'), None when empty. Raises ValueError otherwise."""
    text = (u'{0}'.format(text) if text is not None else u'').strip().upper().replace(u'A', u'').replace(u',', u'.').strip()
    return float(text) if text else None


class Rule(object):
    """A project requirement: the MINIMUM breaker rating for circuits of this load type."""

    def __init__(self, text, rating_a):
        self.text = (text or u'').strip()
        self.rating_a = float(rating_a)
        # whole words only: "AC" is found in "AC - HVAC" but not inside "SPACE" or "HVAC"
        self._pattern = re.compile(u'(?<![A-Za-z0-9])' + re.escape(self.text) + u'(?![A-Za-z0-9])', re.IGNORECASE | re.UNICODE)

    def matches(self, text):
        return bool(self.text) and bool(text) and self._pattern.search(text) is not None

    def __repr__(self):
        return u'{0}: {1:g} A'.format(self.text, self.rating_a)


def match_rule(rules, *texts):
    """The rule whose text is found in one of the texts; the longest text wins
    ("Emergency lighting" before "Lighting"). None when no rule fits."""
    best = None
    for rule in rules:
        if any(rule.matches(text) for text in texts):
            if best is None or len(rule.text) > len(best.text):
                best = rule
    return best


def needed_rating(current_a, margin, ratings, rule_rating=None):
    """The breaker the circuit should have: the rating of its load type, or - when the load
    needs more than that - the next rating of the list above current x margin. None = not known."""
    if current_a is None:
        return None
    if rule_rating is not None and current_a * margin <= rule_rating + 1e-9:
        return rule_rating
    return propose(current_a, margin, ratings)


class Line(object):
    """One circuit in the result table."""

    def __init__(self, panel, number, name, poles, load_w, voltage_v, rating_a, revit_current_a=None, element_id=None):
        self.panel, self.number, self.name, self.poles = panel, number, name, poles
        self.load_w, self.voltage_v, self.rating_a = load_w, voltage_v, rating_a
        self.revit_current_a, self.element_id = revit_current_a, element_id
        self.classification = u''            # load classification of the circuit in Revit
        self.current_a = load_current(load_w, voltage_v, poles)
        self.required_a = None               # current x margin: what the breaker has to carry
        self.rule = None                     # the load-type rule that applies to this circuit
        self.result, self.used = OK, None
        self.remark = u''
        self.proposed = None                 # new rating suggested
        self.outcome = u''                   # what happened when it was written

    def check(self, margin, ratings=(), pf=1.0, rules=()):
        self.current_a = load_current(self.load_w, self.voltage_v, self.poles, pf)
        self.required_a = self.current_a * margin if self.current_a is not None else None
        self.result, self.used = judge(self.current_a, self.rating_a, margin)
        self.remark = u''
        self.proposed = None
        self.rule = match_rule(rules, self.classification, self.name)
        if getattr(self, 'unreadable', None):              # the circuit could not be read from Revit
            self.result, self.used, self.remark = CANNOT_READ, None, self.unreadable
            return self
        if self.result in (NO_VOLTAGE, NO_LOAD):
            return self
        floor = self.rule.rating_a if self.rule is not None else None
        needed = needed_rating(self.current_a, margin, ratings, floor)
        if self.result in (UNDERSIZED, NO_RATING):
            self.proposed = needed
            if needed is None:
                self.remark = u'no rating in your list is large enough' if ratings else u'enter the available ratings'
            elif floor is not None and needed > floor + 1e-9:
                self.remark = u'the load needs more than the minimum {0:g} A for "{1}"'.format(floor, self.rule.text)
        elif floor is not None and self.rating_a + 1e-9 < floor:
            # enough for the load, but below what the project requires for this load type
            self.result, self.proposed = UNDERSIZED, needed
            self.remark = u'below the minimum {0:g} A for "{1}"'.format(floor, self.rule.text)
        elif needed is not None and self.rating_a > needed + 1e-9:
            self.result, self.proposed = OVERSIZED, needed
        elif needed is not None and floor is not None and needed > floor + 1e-9:
            self.remark = u'the load needs more than the minimum {0:g} A for "{1}"'.format(floor, self.rule.text)
        return self

def sort_lines(lines):
    return sorted(lines, key=lambda l: (ORDER.index(l.result), l.panel, l.number))


def counts(lines):
    out = {}
    for line in lines:
        out[line.result] = out.get(line.result, 0) + 1
    return out
