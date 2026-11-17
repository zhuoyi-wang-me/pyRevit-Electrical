# -*- coding: utf-8 -*-
"""Logging framework for the MEP Level Fixer.

Every run writes a timestamped log file to::

    %APPDATA%\\pyRevit\\MEPLevelFixer\\logs\\mepleveler_<YYYYmmdd_HHMMSS>.log

The same records are mirrored to the pyRevit output window when the tool runs
in DEBUG mode, so support engineers can ask a user for one file that contains
everything needed to reproduce an issue.
"""

import os
import logging
from datetime import datetime

LOGGER_NAME = 'mepleveler'
_LOG_FORMAT = '%(asctime)s | %(levelname)-8s | %(name)s | %(message)s'

_session_logger = None
_session_logfile = None


def data_dir():
    """Root folder used for settings and logs (created on demand)."""
    base = os.environ.get('APPDATA') or os.path.expanduser('~')
    folder = os.path.join(base, 'pyRevit', 'MEPLevelFixer')
    if not os.path.isdir(folder):
        try:
            os.makedirs(folder)
        except Exception:
            pass
    return folder


def logs_dir():
    folder = os.path.join(data_dir(), 'logs')
    if not os.path.isdir(folder):
        try:
            os.makedirs(folder)
        except Exception:
            pass
    return folder


def reports_dir():
    folder = os.path.join(data_dir(), 'reports')
    if not os.path.isdir(folder):
        try:
            os.makedirs(folder)
        except Exception:
            pass
    return folder


def get_logger(debug=False):
    """Return the singleton session logger, creating it on first use."""
    global _session_logger, _session_logfile
    if _session_logger is not None:
        _session_logger.setLevel(logging.DEBUG if debug else logging.INFO)
        return _session_logger

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG if debug else logging.INFO)
    logger.propagate = False

    # Remove handlers left over from a previous run in the same Revit session.
    for handler in list(logger.handlers):
        try:
            handler.close()
        except Exception:
            pass
        logger.removeHandler(handler)

    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    _session_logfile = os.path.join(logs_dir(), 'mepleveler_%s.log' % stamp)
    try:
        file_handler = logging.FileHandler(_session_logfile, mode='w')
        file_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
        file_handler.setLevel(logging.DEBUG)
        logger.addHandler(file_handler)
    except Exception:
        # Never let logging failures break the tool.
        logger.addHandler(logging.NullHandler())

    _session_logger = logger
    return logger


def logfile_path():
    """Path of the log file for the current session (may be None)."""
    return _session_logfile


def purge_old_logs(keep=25):
    """Keep only the newest *keep* log files to avoid unbounded growth."""
    try:
        folder = logs_dir()
        files = [os.path.join(folder, f) for f in os.listdir(folder)
                 if f.lower().endswith('.log')]
        files.sort(key=os.path.getmtime, reverse=True)
        for stale in files[keep:]:
            try:
                os.remove(stale)
            except Exception:
                pass
    except Exception:
        pass
