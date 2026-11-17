# -*- coding: utf-8 -*-
"""
ProSheets-style DWG Raster-to-OLE Auto-Binder — exporter-integration build.

Import and call `process_dwg_to_ole(dwg_path)` from your exporter after a
DWG has been written to disk. Call `shutdown_autocad()` once at the very
end of the whole export batch (not per file).

    from dwg_ole_binder import process_dwg_to_ole, shutdown_autocad
    ...
    result = process_dwg_to_ole(dwg_path)
    if not result["success"]:
        errors.append("OLE bind [{}]: {}".format(dwg_path, result.get("warnings")))
    ...
    shutdown_autocad()   # once, at the end of the whole run

CHANGES IN THIS BUILD (exporter-integration pass)
--------------------------------------------------
1. Whole-database fast skip: checks the ACAD_IMAGE_DICT (the same list
   shown in External References) BEFORE doing any block/layout walking.
   If a DWG has zero image definitions anywhere - which becomes the
   common case once a title block has been migrated once - the entire
   scan is skipped in one cheap call instead of walking any block content
   at all. This is the real "why doesn't ProSheets feel slow every time"
   answer: it's not a smarter search, it's not searching when there's
   nothing left to find.
2. BLOCK_SCAN_TIMEOUT_SEC restored to a safe default (90s, matching the
   real observed worst case) and exposed as a parameter, instead of the
   15s that would silently truncate discovery on exactly this kind of
   file.
3. Scan diagnostics (errors, timeout flag, per-container counts) are
   returned to the caller instead of only printed.
4. process_dwg_to_ole() now returns a structured result dict instead of
   a bare True/False - success, converted count, total count, per-item
   failures, timeout flag, and warnings.
5. If the scan hit its timeout, the DWG is NOT converted or saved at all -
   a partial raster list is worse than no conversion, since it would
   silently commit an incomplete result with no backup. The caller gets a
   clear "scan_timed_out" warning back to decide what to do (retry, skip,
   log, raise the timeout for that file).
6. AutoCAD session is cached at module level and reused across every call
   in the same process, instead of re-resolving/relaunching per file.
   Visible=True is kept (required - Visible=False was tried and broke
   SelectionSet.Select and likely other operations that depend on a real
   document window/viewport existing); WindowState is minimized instead
   as a best-effort way to stay out of the way during batch use.
   shutdown_autocad() explicitly closes it once the whole batch is done.
"""

import clr
import os
import time
import traceback
import System

clr.AddReference("System")
clr.AddReference("System.Drawing")
clr.AddReference("System.Runtime.InteropServices")

from System import Type, Activator
from System.Drawing import Image, Bitmap, Graphics
from System.Drawing.Drawing2D import InterpolationMode
from System.IO import File, MemoryStream
from System.Runtime.InteropServices import Marshal
import System.Threading as Threading
try:
    import Queue            # IronPython 2.7
except ImportError:         # IronPython 3 engine: the module was renamed
    import queue as Queue

# For clipboard bitmap construction only - no dialogs/forms are used in
# this build, everything here runs headless.
clr.AddReference("System.Windows.Forms")
from System.Windows.Forms import Application, Clipboard
clr.AddReference("Microsoft.VisualBasic")
from Microsoft.VisualBasic import Interaction


# --------------------------------------------------------------------------
# Tuning
# --------------------------------------------------------------------------
PASTE_TIMEOUT_SEC = 8.0
PASTE_POLL_SEC = 0.05
REGEN_AT_END_ONLY = False
PREFER_DIRECT_COM = True
DEFAULT_BLOCK_SCAN_TIMEOUT_SEC = 90.0   # restored from the 15s stopgap -
                                         # the real worst case observed was
                                         # ~90s; 15s silently truncated scans
MAX_NESTED_BLOCK_ENTITY_COUNT = 400     # nested sub-blocks (found while
                                         # scanning another block, not
                                         # directly inserted by a layout)
                                         # larger than this are skipped
                                         # rather than walked entity-by-
                                         # entity - protects against a
                                         # pathological block (e.g. a
                                         # "KEYPLAN" symbol with thousands
                                         # of entities) eating the whole
                                         # scan budget for something that
                                         # is almost certainly not a logo
TITLEBLOCK_NAME_HINT = "title"          # case-insensitive substring match
                                         # against block names found in a
                                         # layout - in Revit-exported DWGs,
                                         # embedded raster logos always
                                         # live inside the titleblock
                                         # family instance's block, so
                                         # matching this lets the scan go
                                         # straight there instead of also
                                         # walking every other block a
                                         # layout happens to reference
                                         # (scale bars, north arrows,
                                         # stamps, etc). Falls back to the
                                         # old "scan everything referenced"
                                         # behavior if nothing matches, so
                                         # non-standard naming still works.


# --------------------------------------------------------------------------
# COM helpers (direct access first, reflection fallback)
# --------------------------------------------------------------------------

from System.Reflection import BindingFlags

_GET_FLAGS = BindingFlags.GetProperty | BindingFlags.Public | BindingFlags.Instance
_SET_FLAGS = BindingFlags.SetProperty | BindingFlags.Public | BindingFlags.Instance
_CALL_FLAGS = BindingFlags.InvokeMethod | BindingFlags.Public | BindingFlags.Instance
_ITEM_FLAGS = BindingFlags.InvokeMethod | BindingFlags.GetProperty | BindingFlags.Public | BindingFlags.Instance


def _com_get(obj, prop_name):
    if PREFER_DIRECT_COM:
        try:
            return getattr(obj, prop_name)
        except Exception:
            pass
    return obj.GetType().InvokeMember(prop_name, _GET_FLAGS, None, obj, None)


def _com_set(obj, prop_name, value):
    if PREFER_DIRECT_COM:
        try:
            setattr(obj, prop_name, value)
            return
        except Exception:
            pass
    args = System.Array[System.Object]([value])
    obj.GetType().InvokeMember(prop_name, _SET_FLAGS, None, obj, args)


def _com_call(obj, method_name, *args):
    if PREFER_DIRECT_COM:
        try:
            fn = getattr(obj, method_name)
            if args:
                return fn(*args)
            return fn()
        except Exception:
            pass
    net_args = System.Array[System.Object](list(args)) if args else None
    return obj.GetType().InvokeMember(method_name, _CALL_FLAGS, None, obj, net_args)


def _com_item(collection, index):
    if PREFER_DIRECT_COM:
        try:
            return collection.Item(index)
        except Exception:
            try:
                return collection[index]
            except Exception:
                pass
    return collection.GetType().InvokeMember(
        "Item", _ITEM_FLAGS, None, collection,
        System.Array[System.Object]([index])
    )


def _com_count(collection):
    try:
        return int(getattr(collection, "Count"))
    except Exception:
        return int(_com_get(collection, "Count"))


def to_point_array(seq):
    """Build a COM-friendly 3-double Variant array (x, y, z)."""
    return System.Array[System.Double]([float(seq[0]), float(seq[1]), float(seq[2])])


# --------------------------------------------------------------------------
# AutoCAD connection - cached across calls for the whole batch
# --------------------------------------------------------------------------

_AUTOCAD_APP_CACHE = None


def _set_autocad_visible(app, visible=True):
    try:
        _com_set(app, "Visible", bool(visible))
        return True
    except Exception:
        return False


def _hide_autocad_offscreen(app):
    """
    Keeps AutoCAD in Normal WindowState (acNorm = 1) so viewport/OLE message 
    pumps stay 100% active, but positions the window far off-screen so it 
    does not visually interrupt the user.
    """
    try:
        # acNorm = 1 (DO NOT use acMin / 2 as it freezes message loops)
        _com_set(app, "WindowState", 1) 
        _com_set(app, "Left", -10000)
        _com_set(app, "Top", -10000)
        _com_set(app, "Width", 800)
        _com_set(app, "Height", 600)
    except Exception:
        pass

def _bring_autocad_to_front(app):
    """Temporarily bring AutoCAD to the foreground."""
    try:
        _com_set(app, "Visible", True)
    except Exception:
        pass

    try:
        _com_set(app, "WindowState", 1)   # Normal
    except Exception:
        pass

    try:
        caption = str(_com_get(app, "Caption"))
        Interaction.AppActivate(caption)
    except Exception:
        pass

# --------------------------------------------------------------------------
# Log, and a watchdog for calls into AutoCAD that never come back
# --------------------------------------------------------------------------
LOG = None                    # set by export_dwg: a function(text) that writes one line to dwg_log.txt
_AUTOCAD_OWNED = False        # True when this tool started AutoCAD itself (it was not running before)
SHOW_AFTER_SEC = 40.0         # no answer from AutoCAD for this long: bring it on screen
QUIT_AFTER_SEC = 300.0        # still no answer: close the AutoCAD this tool started, so that Revit is freed


def trace(text):
    try:
        if LOG is not None:
            LOG("    AutoCAD: " + text)
    except Exception:
        pass


class Watchdog(object):
    """Guards the calls into AutoCAD.

    Revit calls AutoCAD and waits for its answer. AutoCAD is kept off screen during the export.
    If AutoCAD puts up a dialog (file in use - open read-only?, missing fonts, proxy objects,
    a save question ...), nobody can see it, AutoCAD never answers and Revit waits for ever:
    Revit "hangs". This watchdog runs on a thread of its own, so it keeps working while Revit waits:

      after SHOW_AFTER_SEC without progress   AutoCAD is brought back on screen, in front, so that
                                              the dialog can be seen and answered;
      after QUIT_AFTER_SEC without progress   an AutoCAD that this tool started itself is closed.
                                              The waiting call then fails, the export goes on with
                                              the next sheet and reports the DWG as "images not bound".
                                              An AutoCAD the user had open already is never closed.

    beat(step) is called by the binder after every step: the clock starts again."""

    def __init__(self, hwnd, owned, show_after=None, quit_after=None):
        self.hwnd, self.owned = hwnd, owned
        self.show_after = SHOW_AFTER_SEC if show_after is None else show_after
        self.quit_after = QUIT_AFTER_SEC if quit_after is None else quit_after
        self.step = "starting"
        self.last = time.time()
        self.shown = False
        self.killed = False
        self._done = False
        self._thread = None

    def beat(self, step):
        self.step, self.last = step, time.time()
        trace(step)

    def _show(self):
        """AutoCAD (and a dialog it may have open) back on screen and to the front."""
        try:
            import ctypes
            user32 = ctypes.windll.user32
            hwnd = int(self.hwnd)
            user32.ShowWindow(hwnd, 1)                                  # SW_SHOWNORMAL
            user32.SetWindowPos(hwnd, 0, 60, 60, 1280, 800, 0x0040)    # SWP_SHOWWINDOW
            popup = user32.GetLastActivePopup(hwnd)
            if popup and popup != hwnd:
                user32.SetWindowPos(popup, -1, 160, 160, 0, 0, 0x0001 | 0x0040)   # topmost, keep its size
                user32.SetForegroundWindow(popup)
            else:
                user32.SetForegroundWindow(hwnd)
        except Exception as error:
            trace("could not bring AutoCAD on screen: %s" % error)

    def _quit(self):
        try:
            import ctypes
            from System.Diagnostics import Process
            pid = ctypes.c_ulong(0)
            ctypes.windll.user32.GetWindowThreadProcessId(int(self.hwnd), ctypes.byref(pid))
            if pid.value:
                Process.GetProcessById(int(pid.value)).Kill()
                return True
        except Exception as error:
            trace("could not close AutoCAD: %s" % error)
        return False

    def _watch(self):
        while not self._done:
            time.sleep(0.5)
            waited = time.time() - self.last
            if not self.shown and waited >= self.show_after:
                self.shown = True
                trace("NO ANSWER for %.0f s while '%s' - AutoCAD is brought on screen: a dialog there is probably waiting for an answer" % (waited, self.step))
                self._show()
            if self.owned and not self.killed and waited >= self.quit_after:
                self.killed = True
                trace("still no answer after %.0f s while '%s' - closing the AutoCAD this tool started, to free Revit" % (waited, self.step))
                self._quit()

    def __enter__(self):
        if self.hwnd:
            self._thread = Threading.Thread(Threading.ThreadStart(self._watch))
            self._thread.Name = "DWG_OLE_Watchdog"
            self._thread.IsBackground = True
            self._thread.Start()
        return self

    def __exit__(self, *exc):
        self._done = True
        return False


def _launch_autocad_app():
    """Connect to running AutoCAD or start full AutoCAD through COM.
    Internal - use get_or_start_autocad_app() instead, which caches this."""
    global _AUTOCAD_OWNED
    progids = [
        # the major-version IDs are what AutoCAD registers on this machine ("AutoCAD.Application.25")
        "AutoCAD.Application.26",
        "AutoCAD.Application.25",
        "AutoCAD.Application.24",
        "AutoCAD.Application.23",
        "AutoCAD.Application.26.0",  # AutoCAD 2026
        "AutoCAD.Application.25.2",
        "AutoCAD.Application.25.1",
        "AutoCAD.Application.25.0",  # AutoCAD 2025
        "AutoCAD.Application.24.3",
        "AutoCAD.Application.24.2",  # AutoCAD 2023
        "AutoCAD.Application.24.1",  # AutoCAD 2022
        "AutoCAD.Application.24.0",  # AutoCAD 2021
        "AutoCAD.Application.23.1",  # AutoCAD 2020
        "AutoCAD.Application",       # Default fallback
    ]

    for progid in progids:
        try:
            app = Marshal.GetActiveObject(progid)
            if app is not None:
                _AUTOCAD_OWNED = False                    # the user's own AutoCAD: never closed by the watchdog
                return app
        except Exception:
            pass

    for progid in progids:
        try:
            t = Type.GetTypeFromProgID(progid)
            if t is None:
                continue
            app = Activator.CreateInstance(t)
            for _ in range(20):
                if _set_autocad_visible(app, True):
                    break
                time.sleep(0.25)
            _AUTOCAD_OWNED = True
            return app
        except Exception:
            continue

    raise Exception(
        "Could not start/connect to AutoCAD through COM.\n\n"
        "Make sure FULL AutoCAD is installed and licensed.\n"
        "AutoCAD LT is not supported for this workflow."
    )


def get_or_start_autocad_app():
    global _AUTOCAD_APP_CACHE
    if _AUTOCAD_APP_CACHE is not None:
        try:
            _ = _com_get(_AUTOCAD_APP_CACHE, "Visible")
            return _AUTOCAD_APP_CACHE
        except Exception:
            _AUTOCAD_APP_CACHE = None

    app = _launch_autocad_app()
    _set_autocad_visible(app, True)
    
    # FIX: Move offscreen instead of minimizing
    _hide_autocad_offscreen(app) 
    
    _AUTOCAD_APP_CACHE = app
    return app


def shutdown_autocad():
    """Explicitly close the cached AutoCAD session. Call this ONCE, at the
    very end of the whole export batch - not per file - otherwise every
    file would pay AutoCAD's startup cost again."""
    global _AUTOCAD_APP_CACHE
    if _AUTOCAD_APP_CACHE is not None:
        try:
            _com_call(_AUTOCAD_APP_CACHE, "Quit")
        except Exception:
            pass
        _AUTOCAD_APP_CACHE = None


def wait_for_dwg_ready(doc, timeout=30.0):
    """Poll until AutoCAD exposes the drawing database reliably."""
    start = time.time()
    last_error = None
    while (time.time() - start) < timeout:
        try:
            _ = str(_com_get(doc, "Name"))
            model = _com_get(doc, "ModelSpace")
            _ = _com_count(model)
            return True
        except Exception as ex:
            last_error = ex
            time.sleep(0.10)
    print("DWG ready timeout: %s" % last_error)
    return False


def open_dwg(app, dwg_path):
    """Open DWG, then poll until the database is ready."""
    docs = _com_get(app, "Documents")
    target = os.path.normcase(os.path.abspath(dwg_path))

    try:
        count = _com_count(docs)
    except Exception:
        count = 0

    for i in range(count):
        try:
            d = _com_item(docs, i)
            full_name = str(_com_get(d, "FullName"))
            if os.path.normcase(os.path.abspath(full_name)) == target:
                try:
                    _com_call(d, "Activate")
                except Exception:
                    pass
                wait_for_dwg_ready(d)
                return d
        except Exception:
            pass

    doc = _com_call(docs, "Open", dwg_path)
    try:
        _com_call(doc, "Activate")
    except Exception:
        pass

    if not wait_for_dwg_ready(doc):
        raise Exception("AutoCAD opened the DWG but the drawing database did not become ready within 30 seconds.")
    return doc


def send_command(doc, command_text):
    """Queue an AutoCAD command without an arbitrary fixed delay."""
    return _com_call(doc, "SendCommand", command_text)


def wait_for_entity_count(space_obj, count_before, timeout=PASTE_TIMEOUT_SEC):
    """Poll until AutoCAD finishes PASTECLIP and adds an entity."""
    start = time.time()
    last_count = count_before
    while (time.time() - start) < timeout:
        try:
            # Yield execution to allow Windows/COM messages to process
            Application.DoEvents() 
            
            last_count = _com_count(space_obj)
            if last_count > count_before:
                return last_count
        except Exception:
            pass
        time.sleep(PASTE_POLL_SEC)
    return last_count


def get_cmdactive(doc):
    """Return CMDACTIVE as an int, or None if it could not be read."""
    try:
        return int(_com_call(doc, "GetVariable", "CMDACTIVE"))
    except Exception:
        return None


def wait_for_command_idle(doc, timeout=PASTE_TIMEOUT_SEC):
    """Wait for AutoCAD's command stack to actually return to idle."""
    start = time.time()
    while (time.time() - start) < timeout:
        Application.DoEvents()  # Keeps Windows COM message queue processing
        state = get_cmdactive(doc)
        if state is None:
            time.sleep(PASTE_POLL_SEC)
            return True
        if state == 0:
            return True
        time.sleep(PASTE_POLL_SEC)
    return False


def ensure_idle_before_paste(doc):
    """Make sure no command from a PREVIOUS image is still open and
    waiting on input before we start the next one."""
    if wait_for_command_idle(doc, timeout=1.0):
        return
    try:
        _com_call(doc, "SendCommand", chr(27) + chr(27))
    except Exception:
        pass
    wait_for_command_idle(doc, timeout=2.0)


def _doc_regen(doc, mode=1):
    try:
        _com_call(doc, "Regen", mode)
    except Exception:
        pass


def save_dwg(doc):
    try:
        _com_call(doc, "Save")
        return True
    except Exception as ex:
        print("DWG save failed: %s" % ex)
        return False


def has_any_image_defs(doc):
    """Whole-database, cheap check: does this DWG have ANY raster image
    definition anywhere at all - the same list shown in External
    References? If not, there is nothing to find in Model Space, Paper
    Space, or any block, so the entire (expensive, entity-by-entity)
    block/layout walk can be skipped outright.

    This is the real fast path for repeat exporter runs: once a title
    block has been migrated to OLE once, this returns False immediately
    on every subsequent export of that file, instead of re-walking the
    block's full entity list only to find zero rasters inside it.

    Falls back to "assume yes" (don't skip) on any COM error, so this
    optimization can never cause a real raster to be missed."""
    try:
        database = _com_get(doc, "Database")
        dictionaries = _com_get(database, "Dictionaries")
        image_dict = _com_call(dictionaries, "Item", "ACAD_IMAGE_DICT")
        count = _com_count(image_dict)
        return count > 0
    except Exception:
        return True


# --------------------------------------------------------------------------
# Clipboard - one persistent STA worker instead of one thread per image
# --------------------------------------------------------------------------

CLIPBOARD_TIMEOUT_SEC = 20.0


class ClipboardWorker(object):
    def __init__(self):
        self._queue = Queue.Queue()
        self._stop = False
        self._thread = Threading.Thread(Threading.ThreadStart(self._run))
        self._thread.Name = "DWG_OLE_Clipboard_STA"
        self._thread.IsBackground = True
        self._thread.SetApartmentState(Threading.ApartmentState.STA)
        self._thread.Start()

    def _run(self):
        while True:
            job = self._queue.get()
            if job is None:
                break
            bytes_data, target_w, target_h, result = job
            try:
                bmp = build_aspect_matched_bitmap(bytes_data, target_w, target_h)
                Clipboard.SetImage(bmp)
                result[0] = True
                result[1] = None
            except Exception as ex:
                result[0] = True
                result[1] = ex

    def set_image(self, bytes_data, target_w, target_h):
        result = [False, None]
        self._queue.put((bytes_data, target_w, target_h, result))
        # FIX: this wait had no end. If the clipboard thread had stopped (or another program kept
        # the clipboard locked), Revit waited here for ever.
        end = time.time() + CLIPBOARD_TIMEOUT_SEC
        while not result[0]:
            if time.time() > end or not self._thread.IsAlive:
                raise Exception("The Windows clipboard did not accept the image within %.0f s "
                                "(another program may be holding the clipboard)." % CLIPBOARD_TIMEOUT_SEC)
            time.sleep(0.005)
        if result[1] is not None:
            raise result[1]

    def close(self):
        if self._stop:
            return
        self._stop = True
        self._queue.put(None)
        try:
            self._thread.Join(2000)
        except Exception:
            pass


_CLIPBOARD_WORKER = None


def get_clipboard_worker():
    global _CLIPBOARD_WORKER
    if _CLIPBOARD_WORKER is None:
        _CLIPBOARD_WORKER = ClipboardWorker()
    return _CLIPBOARD_WORKER


def build_aspect_matched_bitmap(bytes_data, target_width, target_height, max_dim=2000):
    """Return a new Bitmap whose PIXEL aspect ratio matches the raster's
    real target_width:target_height. See module history: AutoCAD's OLE
    frame resists an independent post-paste X/Y resize, so the correct
    aspect ratio is baked into the bitmap up front instead."""
    ms = MemoryStream(bytes_data)
    src = Image.FromStream(ms)
    try:
        aspect = float(target_width) / float(target_height)
        if aspect >= 1.0:
            new_w = max_dim
            new_h = int(round(max_dim / aspect))
        else:
            new_h = max_dim
            new_w = int(round(max_dim * aspect))
        new_w = max(new_w, 1)
        new_h = max(new_h, 1)

        out_bmp = Bitmap(new_w, new_h)
        g = Graphics.FromImage(out_bmp)
        try:
            g.InterpolationMode = InterpolationMode.HighQualityBicubic
            g.DrawImage(src, 0, 0, new_w, new_h)
        finally:
            g.Dispose()
        return out_bmp
    finally:
        src.Dispose()
        ms.Dispose()


def set_clipboard_bitmap(bytes_data, target_width, target_height):
    get_clipboard_worker().set_image(bytes_data, target_width, target_height)


def close_clipboard_worker():
    global _CLIPBOARD_WORKER
    if _CLIPBOARD_WORKER is not None:
        _CLIPBOARD_WORKER.close()
        _CLIPBOARD_WORKER = None


# --------------------------------------------------------------------------
# Step 1: auto-discover raster images
# --------------------------------------------------------------------------

def find_raster_images(doc, diagnostics=None, block_scan_timeout_sec=DEFAULT_BLOCK_SCAN_TIMEOUT_SEC):
    """Find raster images, with explicit titleblock/layout support.

    Strategy:
      1. Find direct RasterImage objects in Model/Paper Space using filters.
      2. Walk Layout.Block containers directly (titleblocks are block
         references inside a layout block).
      3. Resolve each titleblock INSERT to its actual block definition and
         recursively scan only reachable nested block definitions.
      4. Also scan direct raster entities encountered inside a layout block.

    diagnostics, if a dict is passed, is populated with counts/errors/types
    AND a "timed_out" boolean - the caller uses that flag to decide whether
    the result is trustworthy enough to act on.
    """
    results = []
    scan_start = time.time()
    max_seconds = block_scan_timeout_sec
    scanned_blocks = set()
    pending_blocks = []
    pending_names = set()
    block_cache = {}

    if diagnostics is not None:
        diagnostics["timed_out"] = False

    def add_error(text):
        if diagnostics is not None:
            diagnostics.setdefault("errors", []).append(str(text))

    def mark_timeout(text):
        add_error(text)
        if diagnostics is not None:
            diagnostics["timed_out"] = True

    def record_raster(ent, space_name, block_obj=None):
        try:
            try:
                raster_width = float(_com_get(ent, "ImageWidth"))
                raster_height = float(_com_get(ent, "ImageHeight"))
            except Exception:
                raster_width = float(_com_get(ent, "Width"))
                raster_height = float(_com_get(ent, "Height"))

            def_name = None
            try:
                def_name = str(_com_get(ent, "Name"))
            except Exception:
                pass

            results.append({
                "entity": ent,
                "space": space_name,
                "block_obj": block_obj,
                "path": str(_com_get(ent, "ImageFile")),
                "def_name": def_name,
                "origin": list(_com_get(ent, "Origin")),
                "width": raster_width,
                "height": raster_height,
                "rotation": float(_com_get(ent, "Rotation")),
                "layer": str(_com_get(ent, "Layer")),
                "handle": str(_com_get(ent, "Handle")),
            })
            return True
        except Exception as ex:
            add_error("Raster properties failed: %s" % ex)
            return False

    def get_block_by_name(name):
        if not name:
            return None
        if name in block_cache:
            return block_cache[name]
        try:
            blocks = _com_get(doc, "Blocks")
            blk = _com_item(blocks, name)
            block_cache[name] = blk
            return blk
        except Exception:
            try:
                blocks = _com_get(doc, "Blocks")
                blk = blocks.GetType().InvokeMember(
                    "Item", _ITEM_FLAGS, None, blocks,
                    System.Array[System.Object]([name])
                )
                block_cache[name] = blk
                return blk
            except Exception as ex:
                add_error("Could not resolve block '%s': %s" % (name, ex))
                return None

    def block_is_xref(blk):
        try:
            return bool(_com_get(blk, "IsXRef"))
        except Exception:
            return False

    def enqueue_block(name, allow_large=True):
        if not name or name in scanned_blocks or name in pending_names:
            return
        blk = get_block_by_name(name)
        if blk is None or block_is_xref(blk):
            return
        if not allow_large:
            try:
                cnt = _com_count(blk)
            except Exception:
                cnt = 0
            if cnt > MAX_NESTED_BLOCK_ENTITY_COUNT:
                add_error(
                    "Skipped nested block '%s' (%d entities > %d limit) - "
                    "too large to plausibly be a logo/graphic, not walked." %
                    (name, cnt, MAX_NESTED_BLOCK_ENTITY_COUNT)
                )
                return
        pending_names.add(name)
        pending_blocks.append(name)

    def entity_block_name(ent):
        name = None
        try:
            name = str(_com_get(ent, "EffectiveName"))
        except Exception:
            pass
        if not name:
            try:
                name = str(_com_get(ent, "Name"))
            except Exception:
                pass
        return name

    def scan_block(block_obj, block_name):
        if block_obj is None:
            return
        if (time.time() - scan_start) > max_seconds:
            mark_timeout("Titleblock/nested block scan exceeded %d seconds." % int(max_seconds))
            return
        if block_name in scanned_blocks:
            return

        scanned_blocks.add(block_name)
        try:
            count = _com_count(block_obj)
        except Exception as ex:
            add_error("Block '%s' count failed: %s" % (block_name, ex))
            return

        if diagnostics is not None:
            diagnostics.setdefault("counts", {})["Block:" + block_name] = count

        for i in range(count):
            if (time.time() - scan_start) > max_seconds:
                mark_timeout("Titleblock/nested block scan exceeded %d seconds while scanning '%s'." %
                             (int(max_seconds), block_name))
                return
            try:
                ent = _com_item(block_obj, i)
                oname = str(_com_get(ent, "ObjectName"))
                low = oname.lower()
                if diagnostics is not None:
                    hist = diagnostics.setdefault("types", {})
                    hist[oname] = hist.get(oname, 0) + 1

                if low == "acdbrasterimage":
                    record_raster(ent, "Block:" + block_name, block_obj)
                    continue

                if low == "acdbblockreference":
                    child = entity_block_name(ent)
                    if child:
                        enqueue_block(child, allow_large=False)
            except Exception as ex:
                add_error("Block '%s' item %d failed: %s" % (block_name, i, ex))

    # 1) Direct raster images in Model and current Paper Space.
    for space_obj, space_name in [
        (_com_get(doc, "ModelSpace"), "Model"),
        (_com_get(doc, "PaperSpace"), "Paper"),
    ]:
        try:
            activate_space(doc, space_name, None)
            selection_sets = _com_get(doc, "SelectionSets")
            set_name = "OLE_Raster_%s" % space_name
            try:
                existing = _com_call(selection_sets, "Item", set_name)
                _com_call(existing, "Delete")
            except Exception:
                pass
            ss = _com_call(selection_sets, "Add", set_name)
            # FIX: acSelectionSetAll returns the objects of model AND paper space together, so the
            # "Model" pass and the "Paper" pass each found EVERY image - a sheet logo was recorded
            # as a model-space image (and pasted into model space at sheet coordinates), then again
            # as a paper-space one. DXF group 67 says which space: 0 = model, 1 = paper.
            filter_type = System.Array[System.Int16]([0, 67])
            filter_data = System.Array[System.Object](["RASTERIMAGE", 0 if space_name == "Model" else 1])
            _com_call(ss, "Select", 7, None, None, filter_type, filter_data)
            count = _com_count(ss)
            if diagnostics is not None:
                diagnostics.setdefault("counts", {})[space_name + ":raster"] = count
            for i in range(count):
                try:
                    record_raster(_com_item(ss, i), space_name, None)
                except Exception as ex:
                    add_error("%s raster item %d failed: %s" % (space_name, i, ex))
            try:
                _com_call(ss, "Delete")
            except Exception:
                pass
        except Exception as ex:
            add_error("%s raster selection failed: %s" % (space_name, ex))

    # 2) Titleblock-aware traversal: inspect Layout.Block directly.
    title_block_names = []      # names containing TITLEBLOCK_NAME_HINT
    fallback_block_names = []   # everything else, only used if none match
    try:
        layouts = _com_get(doc, "Layouts")
        layout_count = _com_count(layouts)
        if diagnostics is not None:
            diagnostics["layout_count"] = layout_count

        for li in range(layout_count):
            if (time.time() - scan_start) > max_seconds:
                mark_timeout("Layout/titleblock scan exceeded %d seconds." % int(max_seconds))
                break
            try:
                layout = _com_item(layouts, li)
                lname = str(_com_get(layout, "Name"))
                if lname.lower() == "model":
                    continue

                layout_block = _com_get(layout, "Block")
                layout_block_name = "Layout:" + lname

                try:
                    layout_count_entities = _com_count(layout_block)
                except Exception:
                    layout_count_entities = 0

                if diagnostics is not None:
                    diagnostics.setdefault("counts", {})[layout_block_name] = layout_count_entities

                for ei in range(layout_count_entities):
                    if (time.time() - scan_start) > max_seconds:
                        mark_timeout("Layout/titleblock scan exceeded %d seconds while scanning layout '%s'." %
                                     (int(max_seconds), lname))
                        break
                    try:
                        ent = _com_item(layout_block, ei)
                        oname = str(_com_get(ent, "ObjectName"))
                        low = oname.lower()
                        if diagnostics is not None:
                            hist = diagnostics.setdefault("types", {})
                            hist[oname] = hist.get(oname, 0) + 1

                        if low == "acdbrasterimage":
                            record_raster(ent, "Layout:" + lname, layout_block)
                        elif low == "acdbblockreference":
                            bname = entity_block_name(ent)
                            if bname:
                                if TITLEBLOCK_NAME_HINT in bname.lower():
                                    if bname not in title_block_names:
                                        title_block_names.append(bname)
                                else:
                                    if bname not in fallback_block_names:
                                        fallback_block_names.append(bname)
                    except Exception as ex:
                        add_error("Layout '%s' item %d failed: %s" % (lname, ei, ex))
            except Exception as ex:
                add_error("Layout %d failed: %s" % (li, ex))
    except Exception as ex:
        add_error("Layouts collection failed: %s" % ex)

    # Fast path: if any layout-referenced block name looks like a
    # titleblock, scan ONLY those - skip every other block a layout
    # happens to reference (scale bars, north arrows, stamps, etc) rather
    # than walking all of them. Falls back to the old "scan everything"
    # behavior only if nothing matched, so non-standard naming still works
    # correctly, just without the speed-up.
    if title_block_names:
        if diagnostics is not None:
            diagnostics["titleblock_fast_path"] = True
            diagnostics["titleblock_names_matched"] = title_block_names
        for name in title_block_names:
            enqueue_block(name, allow_large=True)
    else:
        if diagnostics is not None:
            diagnostics["titleblock_fast_path"] = False
        for name in fallback_block_names:
            enqueue_block(name, allow_large=True)

    # 3) Recursively scan only the blocks actually referenced by layouts.
    while pending_blocks:
        if (time.time() - scan_start) > max_seconds:
            mark_timeout("Titleblock/nested block traversal exceeded %d seconds." % int(max_seconds))
            break
        bname = pending_blocks.pop(0)
        pending_names.discard(bname)
        blk = get_block_by_name(bname)
        if blk is not None and not block_is_xref(blk):
            scan_block(blk, bname)

    # FIX: the fast path scanned ONLY blocks whose name contains "title". When one such block exists
    # (a "Title text" symbol, say) but the logo sits in the real titleblock under another name, nothing
    # was found, the file was reported as clean, and the images stayed linked. When the title-named
    # blocks hold no image, the other blocks of the layouts are scanned as well.
    if title_block_names and not any(r["space"].startswith("Block:") for r in results):
        if diagnostics is not None:
            diagnostics["titleblock_fast_path_fell_back"] = True
        for name in fallback_block_names:
            enqueue_block(name, allow_large=True)
        while pending_blocks:
            if (time.time() - scan_start) > max_seconds:
                mark_timeout("Fallback block traversal exceeded %d seconds." % int(max_seconds))
                break
            bname = pending_blocks.pop(0)
            pending_names.discard(bname)
            blk = get_block_by_name(bname)
            if blk is not None and not block_is_xref(blk):
                scan_block(blk, bname)

    # FIX: the same image could be recorded more than once (the paper-space selection AND the walk of
    # its layout). Converting it twice pasted a second OLE copy and then failed to delete the raster
    # that was already gone. One record per handle; the most exact place wins: a named layout over
    # "Paper" (which only means "the active layout").
    rank = lambda space: 0 if space.startswith("Layout:") else (1 if space == "Model" else
                                                                (2 if space.startswith("Block:") else 3))
    unique = {}
    for record in results:
        key = record.get("handle") or id(record)
        kept = unique.get(key)
        if kept is None or rank(record["space"]) < rank(kept["space"]):
            unique[key] = record
    duplicates = len(results) - len(unique)
    results = [r for r in results if unique.get(r.get("handle") or id(r)) is r]

    if diagnostics is not None:
        diagnostics["duplicates_removed"] = duplicates
        diagnostics["scanned_referenced_blocks"] = len(scanned_blocks)
        diagnostics["referenced_root_blocks"] = len(scanned_blocks)
        diagnostics["block_scan_seconds"] = round(time.time() - scan_start, 3)

    return results


def activate_space(doc, space_name, active_state=None):
    if active_state is not None and active_state.get("space") == space_name:
        return True
    try:
        if space_name.startswith("Layout:"):
            # FIX: an image of a named layout was pasted into whichever layout happened to be active
            # (ActiveSpace = paper only picks "paper space", not WHICH layout). Make that layout current.
            wanted = space_name[len("Layout:"):]
            layouts = _com_get(doc, "Layouts")
            for index in range(_com_count(layouts)):
                layout = _com_item(layouts, index)
                if str(_com_get(layout, "Name")) == wanted:
                    _com_set(doc, "ActiveLayout", layout)
                    break
        _com_set(doc, "ActiveSpace", 1 if space_name == "Model" else 0)
        if active_state is not None:
            active_state["space"] = space_name
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------
# Steps 2-4: convert one raster image to OLE, in place
# --------------------------------------------------------------------------

def build_scale_matrix(sx, sy, base_point):
    m = System.Array.CreateInstance(System.Double, 4, 4)
    for r in range(4):
        for c in range(4):
            m[r, c] = 1.0 if r == c else 0.0
    m[0, 0] = sx
    m[1, 1] = sy
    m[0, 3] = base_point[0] * (1.0 - sx)
    m[1, 3] = base_point[1] * (1.0 - sy)
    return m


def get_entity_bbox(entity):
    flags = BindingFlags.InvokeMethod | BindingFlags.Public | BindingFlags.Instance
    args = System.Array[System.Object]([None, None])
    entity.GetType().InvokeMember("GetBoundingBox", flags, None, entity, args)
    min_pt = [float(v) for v in args[0]]
    max_pt = [float(v) for v in args[1]]
    return min_pt, max_pt


def force_ole_size(ole_ent, target_width, target_height, base_point):
    if target_width <= 0 or target_height <= 0:
        return False

    try:
        min0, max0 = get_entity_bbox(ole_ent)
    except Exception:
        return False

    actual_w = max0[0] - min0[0]
    actual_h = max0[1] - min0[1]
    if actual_w <= 0 or actual_h <= 0:
        return False

    TOL_REL = 0.01
    if abs(actual_w - target_width) <= max(target_width * TOL_REL, 1e-6) and \
       abs(actual_h - target_height) <= max(target_height * TOL_REL, 1e-6):
        return True

    sx = target_width / actual_w
    sy = target_height / actual_h
    matrix = build_scale_matrix(sx, sy, base_point)
    try:
        _com_call(ole_ent, "TransformBy", matrix)
    except Exception:
        try:
            _com_call(ole_ent, "ScaleEntity", to_point_array(base_point), sx)
        except Exception:
            return False

    try:
        min1, max1 = get_entity_bbox(ole_ent)
        final_w = max1[0] - min1[0]
        final_h = max1[1] - min1[1]
        return abs(final_w - target_width) <= max(target_width * 0.02, 1e-6) and \
               abs(final_h - target_height) <= max(target_height * 0.02, 1e-6)
    except Exception:
        return False


def paste_and_position_ole(doc, info, image_cache, staging_space_name, active_state=None):
    path = info["path"]
    if not path or not File.Exists(path):
        return False, None, "Source image file not found on disk: %s" % path

    ensure_idle_before_paste(doc)

    activate_space(doc, staging_space_name, active_state)
    space_obj = _com_get(doc, "ModelSpace" if staging_space_name == "Model" else "PaperSpace")

    try:
        count_before = _com_count(space_obj)
    except Exception:
        return False, None, "Could not read entity count before paste."

    try:
        if path not in image_cache:
            image_cache[path] = File.ReadAllBytes(path)
        set_clipboard_bitmap(image_cache[path], info["width"], info["height"])
    except Exception as ex:
        return False, None, "Clipboard copy failed: %s" % ex

    try:
        _com_call(doc, "SetVariable", "CMDECHO", 0)
    except Exception:
        pass

    # PASTECLIP's "Specify insertion point:" prompt only accepts a 2D
    # point (X,Y) - "0,0,0" gets rejected as "Invalid 2D point." and
    # re-prompts forever.
    try:
        app = _com_get(doc, "Application")
        _bring_autocad_to_front(app)
    except Exception:
        app = None

    send_command(doc, "_.PASTECLIP 0,0 ")

    count_after = wait_for_entity_count(space_obj, count_before)

    if app is not None:
        _hide_autocad_offscreen(app)

    if count_after <= count_before:
        try:
            _com_call(doc, "SendCommand", chr(27) + chr(27))
        except Exception:
            pass
        wait_for_command_idle(doc, timeout=2.0)
        return False, None, "PASTECLIP did not create an entity within %.0fs (command force-cancelled)." % PASTE_TIMEOUT_SEC

    new_ent = _com_item(space_obj, count_after - 1)
    new_name = str(_com_get(new_ent, "ObjectName"))
    if new_name.lower() != "acdbole2frame":
        return False, None, "Pasted entity is '%s', not an OLE object." % new_name

    try:
        origin = [float(c) for c in info["origin"]]
        pt = to_point_array(origin)

        _com_set(new_ent, "Layer", info["layer"])

        try:
            _com_set(new_ent, "LockAspectRatio", False)
        except Exception:
            pass

        _com_set(new_ent, "Width", float(info["width"]))
        _com_set(new_ent, "Height", float(info["height"]))

        # FIX: a raster's Origin is its LOWER-left corner, but an OLE object's InsertionPoint is its
        # UPPER-left corner (AutoCAD ActiveX reference). Setting one to the other put every bound
        # image one image-height too low. Place the unrotated upper-left corner above the origin,
        # then turn about the origin like the raster.
        top_left = to_point_array([origin[0], origin[1] + float(info["height"]), origin[2] if len(origin) > 2 else 0.0])
        _com_set(new_ent, "InsertionPoint", top_left)

        if abs(info["rotation"]) > 1e-9:
            _com_call(new_ent, "Rotate", pt, info["rotation"])
    except Exception as ex:
        return False, new_ent, "OLE created but could not match placement: %s" % ex

    return True, new_ent, "OK"


def convert_one_image(doc, info, image_cache, active_state=None):
    is_block_nested = info["space"].startswith("Block:")
    staging_space = "Paper" if is_block_nested else info["space"]

    ok, new_ent, detail = paste_and_position_ole(doc, info, image_cache, staging_space, active_state)
    if not ok:
        return False, detail

    try:
        _com_call(info["entity"], "Delete")
    except Exception as ex:
        return False, "OLE placed but original RasterImage could not be deleted: %s" % ex

    return True, "OK"


def detach_image_defs(doc, def_names):
    """Detach the given image definitions from External References.
    AutoCAD's -IMAGE Detach option refuses (silently skips) any name still
    genuinely referenced by a remaining entity."""
    names = sorted(set(n for n in def_names if n))
    if not names:
        return
    try:
        _com_call(doc, "SetVariable", "CMDECHO", 0)
    except Exception:
        pass
    for name in names:
        try:
            send_command(doc, "_.-IMAGE\n_D\n%s\n" % name)
            wait_for_command_idle(doc, timeout=3.0)
        except Exception:
            pass


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def _result(success, dwg="", converted=0, total=0, failed=None, warnings=None,
            timed_out=False, skipped_scan=False):
    return {
        "success": success,
        "dwg": dwg,
        "converted": converted,
        "total": total,
        "failed": failed or [],
        "warnings": warnings or [],
        "timed_out": timed_out,
        "skipped_scan": skipped_scan,
    }


def process_dwg_to_ole(dwg, block_scan_timeout_sec=DEFAULT_BLOCK_SCAN_TIMEOUT_SEC):
    """Convert every raster image in `dwg` (Model/Paper Space and any
    reachable block, incl. titleblocks) to an embedded OLE object, then
    save the DWG in place. Returns a result dict - see _result() above.

    Reuses the same cached, invisible AutoCAD session across calls (see
    get_or_start_autocad_app) - does not launch or quit AutoCAD per file.
    Call shutdown_autocad() once, after the whole batch is done.
    """
    if not dwg or not os.path.isfile(dwg):
        return _result(False, dwg, warnings=["DWG not found: %s" % dwg])

    doc = None
    guard = None
    try:
        trace("connecting to AutoCAD")
        app = get_or_start_autocad_app()
        try:
            hwnd = int(_com_get(app, "HWND"))
        except Exception:
            hwnd = 0
        guard = Watchdog(hwnd, _AUTOCAD_OWNED)
        guard.__enter__()
        guard.beat("opening %s" % os.path.basename(dwg))
        doc = open_dwg(app, dwg)
        guard.beat("drawing is open; looking for images")

        try:
            _com_call(doc, "SetVariable", "OLEFRAME", 0)
        except Exception:
            pass

        # Whole-database fast skip - see has_any_image_defs() docstring.
        if not has_any_image_defs(doc):
            try:
                _com_call(doc, "Close", False)
            except Exception:
                pass
            return _result(True, dwg, converted=0, total=0, skipped_scan=True)

        diagnostics = {}
        guard.beat("scanning the drawing and its blocks for raster images")
        rasters = find_raster_images(doc, diagnostics, block_scan_timeout_sec)
        guard.beat("%d image(s) found" % len(rasters))
        try:
            places = {}
            for record in rasters:
                places[record["space"]] = places.get(record["space"], 0) + 1
            trace("images by place: %s; duplicates removed: %s; titleblock blocks: %s%s; %d scan problem(s)" % (
                ", ".join("%s %d" % item for item in sorted(places.items())) or "none",
                diagnostics.get("duplicates_removed", 0),
                ", ".join(diagnostics.get("titleblock_names_matched", [])) or "none matched 'title'",
                " (no image there - other blocks scanned too)" if diagnostics.get("titleblock_fast_path_fell_back") else "",
                len(diagnostics.get("errors", []))))
        except Exception:
            pass

        if diagnostics.get("timed_out"):
            # A partial raster list is worse than none - don't convert or
            # save anything from an incomplete scan. Close untouched and
            # let the caller decide (retry with a longer timeout, skip,
            # log for manual follow-up, etc).
            try:
                _com_call(doc, "Close", False)
            except Exception:
                pass
            return _result(
                False, dwg, converted=0, total=len(rasters),
                warnings=["Raster/block scan exceeded %.0fs and was aborted before "
                          "converting anything - DWG left unmodified." % block_scan_timeout_sec]
                         + diagnostics.get("errors", []),
                timed_out=True
            )

        if not rasters:
            try:
                _com_call(doc, "Close", False)
            except Exception:
                pass
            return _result(
                True, dwg, converted=0, total=0,
                warnings=diagnostics.get("errors", [])
            )

        image_cache = {}
        succeeded = []
        failed = []
        detached_defs = []
        active_state = {"space": None}

        get_clipboard_worker()

        for number, info in enumerate(rasters):
            guard.beat("binding image %d of %d (%s)" % (number + 1, len(rasters), os.path.basename(info.get("path") or "") or info.get("handle")))
            ok, detail = convert_one_image(doc, info, image_cache, active_state)
            label = "%s (%s space)" % (os.path.basename(info["path"]) or info["handle"], info["space"])
            if ok:
                succeeded.append(label)
                detached_defs.append(info.get("def_name"))
            else:
                failed.append("%s -> %s" % (label, detail))

        guard.beat("regenerating and detaching the image references")
        if REGEN_AT_END_ONLY:
            _doc_regen(doc, 1)

        detach_image_defs(doc, detached_defs)
        close_clipboard_worker()
        guard.beat("saving the drawing")

        if not succeeded:
            try:
                _com_call(doc, "Close", False)
            except Exception:
                pass
            return _result(
                False, dwg, converted=0, total=len(rasters),
                failed=failed,
                warnings=["No raster image could be converted to OLE."]
            )

        try:
            _com_call(doc, "SetVariable", "ISAVEBAK", 0)
        except Exception:
            pass

        if not save_dwg(doc):
            try:
                _com_call(doc, "Close", False)
            except Exception:
                pass
            return _result(
                False, dwg, converted=len(succeeded), total=len(rasters),
                failed=failed,
                warnings=["OLE conversion completed, but the DWG could not be saved."]
            )

        try:
            _com_call(doc, "Close", False)
        except Exception as ex:
            print("Could not close processed DWG: %s" % ex)

        return _result(
            len(failed) == 0, dwg,
            converted=len(succeeded), total=len(rasters),
            failed=failed
        )

    except Exception:
        err = traceback.format_exc()
        print(err)
        if guard is not None and guard.killed:
            # AutoCAD never answered and was closed by the watchdog: say that, not the COM error
            global _AUTOCAD_APP_CACHE
            _AUTOCAD_APP_CACHE = None
            return _result(False, dwg, warnings=[
                "AutoCAD did not answer for %.0f s while %s and was closed so that Revit could go on. "
                "A dialog in AutoCAD was probably waiting for an answer (file in use, missing fonts, proxy objects ...). "
                "The DWG was exported, but its images were NOT bound as OLE. Open the DWG in AutoCAD by hand once to see the dialog."
                % (guard.quit_after, guard.step)])
        try:
            if doc is not None:
                _com_call(doc, "Close", False)
        except Exception:
            pass
        return _result(False, dwg, warnings=[err])

    finally:
        if guard is not None:
            if guard.shown and not guard.killed:
                trace("AutoCAD answered again after it was brought on screen")
            guard.__exit__()
        close_clipboard_worker()