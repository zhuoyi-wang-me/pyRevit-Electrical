# -*- coding: utf-8 -*-
"""Place Views on Sheet - settings (Shift+click the button).

Read-only for the model: only the user's pyRevit settings are changed.
"""
from pyrevit import forms, script

TITLE = 'Place Views on Sheet - settings'

FIELDS = [
    ('spacing_mm', 50.0, u'Minimum spacing between viewports (mm, titles included)'),
    ('margin_left_mm', 20.0, u'Margin inside the title block - LEFT (mm)'),
    ('margin_right_mm', 20.0, u'Margin inside the title block - RIGHT (mm; e.g. 190 for a title strip on the right)'),
    ('margin_top_mm', 20.0, u'Margin inside the title block - TOP (mm)'),
    ('margin_bottom_mm', 20.0, u'Margin inside the title block - BOTTOM (mm; e.g. 60 for a title strip at the bottom)'),
]

cfg = script.get_config()
for name, default, prompt in FIELDS:
    current = cfg.get_option(name, default)
    text = forms.ask_for_string(default=u'{0}'.format(current), title=TITLE, prompt=prompt)
    if text is None:
        script.exit()                                             # cancelled: nothing saved
    try:
        value = float(text.strip().replace(u',', u'.'))
    except ValueError:
        forms.alert(u'"{0}" is not a number - settings not saved.'.format(text), title=TITLE)
        script.exit()
    if value < 0:
        forms.alert(u'Values cannot be negative - settings not saved.', title=TITLE)
        script.exit()
    setattr(cfg, name, value)

order = forms.CommandSwitchWindow.show(
    [u'Tallest views first (tighter rows)', u'In the order listed'],
    message=u'Order of the views on the sheet:')
if order is None:
    script.exit()
cfg.tallest_first = order.startswith(u'Tallest')
script.save_config()
forms.alert(u'Settings saved.', title=TITLE)
