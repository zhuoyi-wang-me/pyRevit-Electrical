# -*- coding: utf-8 -*-
"""ExternalEvent dispatcher for the modeless Export Manager window.

Why this exists
---------------
A modeless WPF window's button handlers run OUTSIDE a Revit API context.
Opening a Transaction or calling Document.Export() from there throws
"Starting a transaction from an external application running outside of
API context is not allowed" (or silently misbehaves).  The only supported
way back into a valid context is an IExternalEventHandler raised through
ExternalEvent.  ui.py queues a callable here; Revit calls Execute() on its
main thread as soon as it is idle, with a valid UIApplication.

The ExternalEvent itself MUST be created while a valid API context exists,
i.e. in ui.run() while the pushbutton command is still executing.
"""

import traceback

from Autodesk.Revit.UI import (
    ExternalEvent,
    ExternalEventRequest,
    IExternalEventHandler
)


class _QueueHandler(IExternalEventHandler):
    """Runs every queued callable(uiapp) in order, inside the API context."""

    def __init__(self, name, on_error=None):
        self._name = name
        self._on_error = on_error
        self.pending = []

    def Execute(self, uiapp):
        jobs = list(self.pending)
        del self.pending[:]
        for job in jobs:
            try:
                job(uiapp)
            except Exception as error:
                if self._on_error is not None:
                    try:
                        self._on_error(error, traceback.format_exc())
                        continue
                    except Exception:
                        pass
                print(traceback.format_exc())

    def GetName(self):
        return self._name


class RevitDispatcher(object):
    """Queue work for Revit's API context from a modeless window."""

    def __init__(self, name="Export Manager", on_error=None):
        self._handler = _QueueHandler(name, on_error)
        self._event = ExternalEvent.Create(self._handler)

    def run(self, job):
        """Queue job(uiapp).  Returns immediately; the job runs when Revit
        is idle (normally within milliseconds)."""
        if self._event is None:
            raise RuntimeError("The Export Manager window has been closed.")
        self._handler.pending.append(job)
        request = self._event.Raise()
        if request not in (ExternalEventRequest.Accepted, ExternalEventRequest.Pending):
            del self._handler.pending[:]
            raise RuntimeError(
                "Revit refused the request ({}). Finish any active command "
                "or dialog in Revit (press Esc) and try again.".format(request)
            )

    @property
    def is_busy(self):
        return bool(self._handler.pending)

    def dispose(self):
        if self._event is not None:
            try:
                self._event.Dispose()
            except Exception:
                pass
        self._event = None
