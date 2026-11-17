# -*- coding: utf-8 -*-
"""Breaker Check - the template: power factor, margin, ratings and the load-type rules in one JSON file.

    {
      "power_factor": "0.95",
      "margin": "1.25",
      "ratings": "6, 10, 16, 20, 25, 32, 40, 63",
      "rules": [ {"load_type": "Emergency lighting", "breaker_a": 6}, {"load_type": "Lighting", "breaker_a": 16} ]
    }
No Revit in this file.
"""
import codecs
import json

import bc_check


class TemplateError(Exception):
    pass


def build(pf_text, margin_text, ratings_text, rules):
    return {'power_factor': pf_text, 'margin': margin_text, 'ratings': ratings_text,
            'rules': [{'load_type': rule.text, 'breaker_a': rule.rating_a} for rule in rules]}


def rules_from(data):
    """[bc_check.Rule] of a template; entries without a text or a rating above 0 are left out."""
    out = []
    for item in (data or {}).get('rules') or []:
        try:
            rule = bc_check.Rule(item.get('load_type'), item.get('breaker_a'))
        except Exception:
            continue
        if rule.text and rule.rating_a > 0:
            out.append(rule)
    return out


def save(path, data):
    with codecs.open(path, 'w', 'utf-8') as handle:
        handle.write(json.dumps(data, indent=2, sort_keys=True))


def load(path):
    try:
        with codecs.open(path, 'r', 'utf-8-sig') as handle:
            data = json.load(handle)
    except Exception as error:
        raise TemplateError(u'The template cannot be read:\n{0}\n{1}'.format(path, error))
    if not isinstance(data, dict) or 'rules' not in data:
        raise TemplateError(u'This file is not a Breaker Check template:\n{0}'.format(path))
    return data
