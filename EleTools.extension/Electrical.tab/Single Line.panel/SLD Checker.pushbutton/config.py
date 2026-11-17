# -*- coding: utf-8 -*-
"""Shift+Click: open the folder holding the SLD Checker templates
(one JSON per template - copy them to share with the team).
Mapping, checks and templates are edited in the setup window (normal click)."""
import os

from sldcheck import config

config.migrate_legacy()
if not os.path.isdir(config.TEMPLATE_DIR):
    os.makedirs(config.TEMPLATE_DIR)
os.startfile(config.TEMPLATE_DIR)
