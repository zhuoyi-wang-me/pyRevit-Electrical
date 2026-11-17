# -*- coding: utf-8 -*-
"""Progress window.

Wraps :class:`pyrevit.forms.ProgressBar` so the engine only has to call a
single callable::

    with ProgressReporter('Analysing') as reporter:
        engine.analyze(elements, progress=reporter.callback)

The callback returns ``False`` when the user presses Cancel, which every loop
in the engine honours.
"""

from pyrevit import forms


class ProgressReporter(object):
    """Context manager exposing an engine friendly progress callback."""

    #: How often the window is refreshed. Updating WPF for every single element
    #: is the single biggest cost on 10k+ element models, so the update is
    #: throttled.
    REFRESH_EVERY = 10

    def __init__(self, caption='Processing', cancellable=True, total=None):
        self.caption = caption
        self.cancellable = cancellable
        self.total = total
        self.cancelled = False
        self._bar = None
        self._last_drawn = -1

    # -- context manager --------------------------------------------------
    def __enter__(self):
        self._bar = forms.ProgressBar(
            title=self.caption + '  |  {value} of {max_value}',
            cancellable=self.cancellable,
            step=1)
        self._bar.__enter__()
        return self

    def __exit__(self, exc_type, exc_value, exc_tb):
        if self._bar is not None:
            self._bar.__exit__(exc_type, exc_value, exc_tb)
            self._bar = None
        return False

    # -- engine callback --------------------------------------------------
    def callback(self, index, total, category=None, label=None):
        """Return False to request cancellation."""
        if self._bar is None:
            return True

        if self._bar.cancelled:
            self.cancelled = True
            return False

        # Throttle: always draw the first, last and every Nth item.
        if index != 0 and index != total - 1 \
                and (index - self._last_drawn) < self.REFRESH_EVERY:
            return True
        self._last_drawn = index

        remaining = max(0, total - index - 1)
        detail = []
        if category:
            detail.append(category)
        if label:
            detail.append(label)
        detail.append('%d left' % remaining)
        self._bar.title = '%s  |  {value} of {max_value}  |  %s' % (
            self.caption, '  -  '.join(detail))
        self._bar.update_progress(index + 1, total)
        return True
