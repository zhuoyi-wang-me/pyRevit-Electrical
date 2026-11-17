# -*- coding: utf-8 -*-
"""Export Manager - a Cancel button that works while Revit is printing.

Why the Cancel of the progress bar seemed dead: while Revit prints or exports a
sheet it does not answer Windows. After a few seconds Windows shows the Revit
windows as "not responding", and a click on such a window is thrown away - it
never reaches the Cancel button. The click only counted in the short moments
between two sheets.

This small window runs on a thread of its own, so it stays alive while Revit is
busy. A click is remembered at once; the export stops as soon as Revit comes
back from the sheet it is working on (a sheet that is being printed cannot be
interrupted half way). The Esc key does NOT cancel (see wanted()).

FIX: a print run of 51 sheets stopped after a few sheets with no message. Esc was read
anywhere in Windows, and a single tap - in Revit while working on during the export
(Esc to clear a selection), or in any other program - cancelled the run. Now Esc must
be HELD for HOLD_SECONDS, and `reason` says what stopped the run (for the print log).

    with CancelWatch() as watch:
        ...
        if watch.cancelled: stop
"""
import time

import clr
for _name in ("PresentationFramework", "PresentationCore", "WindowsBase", "System.Xaml"):
    clr.AddReference(_name)

from System import Action
from System.Threading import ApartmentState, ManualResetEvent, Thread, ThreadStart

VK_ESCAPE = 0x1B
HOLD_SECONDS = 5.0                        # Esc must be held this long to cancel - a tap is not a cancel


def escape_is_down():
    """True while the Esc key is held down (works even when Revit does not answer)."""
    try:
        import ctypes
        return bool(ctypes.windll.user32.GetAsyncKeyState(VK_ESCAPE) & 0x8000)
    except Exception:
        return False


class CancelWatch(object):
    def __init__(self, title="Export Manager", text="Cancel export"):
        self.title, self.text = title, text
        self.cancelled = False
        self.reason = None                 # what stopped the run: 'Cancel button'
        self._esc_since = None             # when Esc was first seen down (it must stay down HOLD_SECONDS)
        self.shown = False                 # False when the window could not be made: the progress bar's own Cancel remains
        self._window = None
        self._button = None
        self._ready = ManualResetEvent(False)
        self._thread = None

    # ---- runs on the thread of the window
    def _run(self):
        try:
            from System.Windows import (HorizontalAlignment, ResizeMode, SystemParameters, Thickness, VerticalAlignment,
                                        Window, WindowStartupLocation, WindowStyle)
            from System.Windows.Controls import Button, StackPanel, TextBlock
            from System.Windows.Threading import Dispatcher, DispatcherPriority

            window = Window()
            window.Title = self.title
            window.Width, window.Height = 300, 128
            window.WindowStyle = WindowStyle.ToolWindow
            window.ResizeMode = ResizeMode.NoResize
            window.Topmost = True
            window.ShowInTaskbar = False
            window.ShowActivated = False
            window.WindowStartupLocation = WindowStartupLocation.Manual
            area = SystemParameters.WorkArea
            window.Left = area.Right - window.Width - 24
            window.Top = area.Top + 90

            panel = StackPanel()
            panel.Margin = Thickness(14, 10, 14, 10)
            hint = TextBlock()
            hint.Text = "Stops after the sheet Revit is working on."
            hint.Margin = Thickness(0, 0, 0, 8)
            button = Button()
            button.Content = self.text
            button.Height = 32
            button.HorizontalAlignment = HorizontalAlignment.Stretch
            button.VerticalAlignment = VerticalAlignment.Center

            def clicked(sender, args):
                self.cancelled = True
                self.reason = "Cancel button"
                button.Content = "Cancelling - waiting for Revit ..."
                button.IsEnabled = False
            button.Click += clicked
            panel.Children.Add(hint)
            panel.Children.Add(button)
            window.Content = panel
            # closing the window ends its thread
            window.Closed += lambda sender, args: window.Dispatcher.BeginInvokeShutdown(DispatcherPriority.Background)
            self._window, self._button = window, button
            window.Show()
            self.shown = True
            self._ready.Set()
            Dispatcher.Run()                                   # until the window is closed
        except Exception:
            self.shown = False
            self._ready.Set()

    # ---- used from Revit's thread
    def __enter__(self):
        try:
            thread = Thread(ThreadStart(self._run))
            thread.SetApartmentState(ApartmentState.STA)
            thread.IsBackground = True
            thread.Start()
            self._thread = thread
            self._ready.WaitOne(3000)
        except Exception:
            self.shown = False
        return self

    def click(self):
        """Press the button from code (used by the tests)."""
        from System.Windows import RoutedEventArgs
        from System.Windows.Controls import Button
        self._window.Dispatcher.Invoke(Action(lambda: self._button.RaiseEvent(RoutedEventArgs(Button.ClickEvent))))

    def wanted(self):
        """True when the user pressed the Cancel button of this window.
        FIX: the Esc key no longer cancels at all (the user's wish) - it was read anywhere in Windows, and Esc
        pressed while working on in Revit stopped long print runs. Only the Cancel buttons stop an export."""
        return self.cancelled

    def __exit__(self, *exc):
        try:
            if self._window is not None:
                self._window.Dispatcher.BeginInvoke(Action(self._window.Close))
            if self._thread is not None:
                self._thread.Join(2000)
        except Exception:
            pass
        return False
