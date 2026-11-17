# -*- coding: utf-8 -*-
"""
Settings dialog for Auto-Place Fixtures (runs on SHIFT+CLICK).
Values are stored in the pyRevit user config (section elecplace_settings).
"""
import os
import sys

from pyrevit import forms

_LIB = os.path.dirname(os.path.abspath(__file__))
while _LIB and not _LIB.lower().endswith(".extension") and os.path.dirname(_LIB) != _LIB:
    _LIB = os.path.dirname(_LIB)                      # up to the *.extension folder (works inside a .stack)
_LIB = os.path.join(_LIB, "lib")
if os.path.isdir(_LIB) and _LIB not in sys.path:
    sys.path.append(_LIB)

from elecplace.settings import DEFAULTS, Settings   # noqa: E402

SAVE = ">> Save and close"
RESET = ">> Reset to defaults"


def _option_label(settings, key, label):
    return u"{}:  {}".format(label, getattr(settings, key))


def main():
    settings = Settings.load()
    while True:
        labels = {}
        for key, _default, label, typ in DEFAULTS:
            labels[_option_label(settings, key, label)] = (key, label, typ)
        options = sorted(labels.keys()) + [RESET, SAVE]
        choice = forms.CommandSwitchWindow.show(
            options, message="Auto-Place Fixtures settings - click an item to change it")
        if choice is None:
            return                                   # closed without saving
        if choice == SAVE:
            settings.save()
            forms.alert("Settings saved.")
            return
        if choice == RESET:
            settings.reset()
            continue

        key, label, typ = labels[choice]
        if typ is bool:
            setattr(settings, key, not getattr(settings, key))   # toggle
            continue
        text = forms.ask_for_string(default=str(getattr(settings, key)),
                                    prompt=label, title="Edit setting")
        if text is None:
            continue
        try:
            value = typ(text.strip())
            if typ is float and value < 0:
                raise ValueError("negative")
            setattr(settings, key, value)
        except Exception:
            forms.alert(u"Invalid value for '{}': {}".format(label, text))


main()
