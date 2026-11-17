# -*- coding: utf-8 -*-
"""
File logging for ElecAutoCircuit.

One log file per day in %APPDATA%/ElecAutoCircuit/logs. DEBUG and above goes to
file (useful for support on large projects); WARNING and above is echoed to the
pyRevit output window via the stream handler.
"""
import datetime
import logging
import os

from autocircuit.config import APP_DIR, ensure_dir

LOG_DIR = os.path.join(APP_DIR, 'logs')
_NAME = 'ElecAutoCircuit'


def log_file_path():
    return os.path.join(LOG_DIR, 'autocircuit_{0}.log'.format(datetime.date.today().strftime('%Y%m%d')))


def get_logger(name=_NAME):
    log = logging.getLogger(name)
    if getattr(log, '_autocircuit_configured', False):
        return log
    log.setLevel(logging.DEBUG)
    log.propagate = False  # keep pyRevit's own logger configuration untouched
    fmt = logging.Formatter('%(asctime)s %(levelname)-7s %(module)-16s %(message)s')
    try:
        ensure_dir(LOG_DIR)
        fh = logging.FileHandler(log_file_path(), encoding='utf-8')
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except Exception:  # read-only profile etc. - never block the tool because of logging
        pass
    sh = logging.StreamHandler()
    sh.setLevel(logging.WARNING)
    sh.setFormatter(logging.Formatter('%(levelname)s: %(message)s'))
    log.addHandler(sh)
    log._autocircuit_configured = True
    return log
