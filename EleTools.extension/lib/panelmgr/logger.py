# -*- coding: utf-8 -*-
"""
Central logging configuration.

pyRevit already provides `pyrevit.script.get_logger()` which writes to
the pyRevit output panel. We wrap it so the rest of the codebase does
not take a hard dependency on pyRevit and can be imported / unit
tested outside Revit (the fallback uses the standard `logging` module
writing to a rotating file under %APPDATA%).
"""

import os
import logging
from logging.handlers import RotatingFileHandler

_LOG_DIR = os.path.join(
    os.environ.get("APPDATA", os.path.expanduser("~")),
    "pyRevit", "PanelScheduleManager", "logs"
)

_initialized = False


_file_handler = None


def _ensure_file_logging():
    """
    Builds (once) the rotating file handler and returns it, or None if
    the log directory could not be created.

    FORTIETH CORRECTION: this used to attach the handler to the
    logger named "panelmgr" and stop there, while `get_logger` went on
    to return pyRevit's OWN logger - which lives under a completely
    different name and is therefore not in the "panelmgr" hierarchy.
    Inside Revit (the only place that matters) nothing this tool logged
    ever reached the file, so the log this module's docstring promises
    for diagnosing field issues was always empty. The handler is now
    attached to whichever logger is actually handed back.
    """
    global _initialized, _file_handler
    if _initialized:
        return _file_handler
    _initialized = True
    try:
        if not os.path.isdir(_LOG_DIR):
            os.makedirs(_LOG_DIR)
        handler = RotatingFileHandler(
            os.path.join(_LOG_DIR, "panel_schedule_manager.log"),
            maxBytes=2 * 1024 * 1024,
            backupCount=3
        )
        handler.setFormatter(logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
        ))
        handler.setLevel(logging.DEBUG)
        _file_handler = handler
    except Exception:
        # Logging must never crash the tool itself.
        _file_handler = None
    return _file_handler


def _attach_file_handler(target_logger):
    handler = _ensure_file_logging()
    if handler is None or target_logger is None:
        return
    try:
        if handler not in (target_logger.handlers or []):
            target_logger.addHandler(handler)
    except Exception:
        pass


class _FileWriter(object):
    """
    FIFTY-THIRD CORRECTION. Writes every message (DEBUG and up) straight to
    the rotating file handler.

    The FIFTY-FIRST CORRECTION gave the file its own `logging` logger at
    DEBUG level, but inside Revit pyRevit installs its own Logger class,
    which applies pyRevit's global WARNING threshold whatever level is set
    on the logger - so the log of a live run still held only warnings,
    and none of the "N circuits moved, imbalance X% -> Y%" lines. Records
    are now built here and handed to the handler directly, so no logger
    level can filter them.
    """

    def __init__(self, name):
        self.name = name

    def _write(self, level, msg, args, exc_info=None):
        handler = _ensure_file_logging()
        if handler is None:
            return
        try:
            import sys
            record = logging.LogRecord(self.name, level, "", 0, msg, args,
                                       sys.exc_info() if exc_info else None)
            handler.handle(record)
        except Exception:
            pass

    def debug(self, msg, *args, **kwargs):
        self._write(logging.DEBUG, msg, args, kwargs.get("exc_info"))

    def info(self, msg, *args, **kwargs):
        self._write(logging.INFO, msg, args, kwargs.get("exc_info"))

    def warning(self, msg, *args, **kwargs):
        self._write(logging.WARNING, msg, args, kwargs.get("exc_info"))

    def error(self, msg, *args, **kwargs):
        self._write(logging.ERROR, msg, args, kwargs.get("exc_info"))

    def critical(self, msg, *args, **kwargs):
        self._write(logging.CRITICAL, msg, args, kwargs.get("exc_info"))

    def exception(self, msg, *args, **kwargs):
        self._write(logging.ERROR, msg, args, True)


def _file_logger():
    """Writes EVERYTHING (DEBUG and up) to the log file."""
    return _FileWriter("panelmgr")


class _TeeLogger(object):
    """
    FIFTY-FIRST CORRECTION. Sends every message to pyRevit's logger (the
    output window) AND to the log file.

    The file handler used to be attached to pyRevit's logger. That logger
    only passes WARNING and above unless pyRevit is in verbose mode, so
    every `logger.info(...)` - which circuits were moved, how many spares
    were placed, the before / after figures - was dropped before it
    reached the file. The log of a real run contained 570 warnings and
    not one line about what the batch actually did. The file now gets
    its own logger at DEBUG level; the output window is unchanged.
    """

    def __init__(self, primary, secondary):
        self._primary = primary
        self._secondary = secondary

    def _both(self, method, msg, args, kwargs):
        try:
            getattr(self._primary, method)(msg, *args, **kwargs)
        except Exception:
            pass
        try:
            getattr(self._secondary, method)(msg, *args, **kwargs)
        except Exception:
            pass

    def debug(self, msg, *args, **kwargs):
        self._both("debug", msg, args, kwargs)

    def info(self, msg, *args, **kwargs):
        self._both("info", msg, args, kwargs)

    def warning(self, msg, *args, **kwargs):
        self._both("warning", msg, args, kwargs)

    warn = warning

    def error(self, msg, *args, **kwargs):
        self._both("error", msg, args, kwargs)

    def critical(self, msg, *args, **kwargs):
        self._both("critical", msg, args, kwargs)

    def exception(self, msg, *args, **kwargs):
        self._both("exception", msg, args, kwargs)

    def __getattr__(self, name):
        # anything else (setLevel, handlers, ...) is pyRevit's logger's
        return getattr(self._primary, name)


def get_logger(name):
    """
    Returns a logger. Tries to use pyRevit's script logger (visible in
    the pyRevit output window) and always also attaches a rotating
    file handler under panelmgr so field issues can be diagnosed from
    log files without reproducing the session.
    """
    try:
        from pyrevit import script as _pyrevit_script
        pyrevit_logger = _pyrevit_script.get_logger()
        return _TeeLogger(pyrevit_logger, _file_logger())
    except Exception:
        logger = logging.getLogger("panelmgr.{}".format(name))
        if not logger.handlers:
            stream = logging.StreamHandler()
            stream.setFormatter(
                logging.Formatter("%(levelname)-8s %(name)s: %(message)s")
            )
            logger.addHandler(stream)
        _attach_file_handler(logger)
        logger.setLevel(logging.DEBUG)
        return logger
