# -*- coding: utf-8 -*-
"""DWG export engine for the pyRevit Export Manager (Revit 2025.4).

This module deliberately uses DWGExportOptions.GetPredefinedOptions() so the
selected Revit DWG Export Setup controls layer mapping, links/Xrefs, units,
colours and all other DWG settings.  It does not create default options that
would ignore the user's setup.

Typical use from ui.py:

    import export_dwg
    import filename_builder

    setup_names = export_dwg.get_setup_names(doc)
    result = export_dwg.export_items(
        doc, selected_items, output_folder, selected_setup_name,
        filename_builder.build_filename
    )
"""

import os
import time

from Autodesk.Revit.DB import (
    DWGExportOptions,
    ElementId,
    ExportDWGSettings,
    FilteredElementCollector
)
from System.Collections.Generic import List

import helpers

LOG_PATH = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "EleTools", "ExportManager", "dwg_log.txt")


def log(text):
    """One line in dwg_log.txt, written and closed at once. If Revit ever stops answering during a
    DWG export, the last line says which sheet it was working on and at which step."""
    try:
        folder = os.path.dirname(LOG_PATH)
        if not os.path.isdir(folder):
            os.makedirs(folder)
        fresh = not os.path.isfile(LOG_PATH) or os.path.getsize(LOG_PATH) > 400000
        with open(LOG_PATH, "w" if fresh else "a") as handle:
            handle.write("{0}  [Revit {1}]  {2}\n".format(time.strftime("%Y-%m-%d %H:%M:%S"), os.getpid(), text))
    except Exception:
        pass


class _DialogWatch(object):
    """While the DWG export runs: every dialog Revit puts up is written to the log (and reported at
    the end). Revit can ask a question in the middle of a DWG export - temporary hide/isolate is on,
    a link is not loaded, a file exists ... - and such a dialog can end up behind other windows:
    Revit then looks hung although it is only waiting for an answer."""

    def __init__(self, uidoc, results):
        self.app = None
        self.results = results
        self.seen = []
        try:
            self.app = uidoc.Application
        except Exception:
            self.app = None

    def _showing(self, sender, args):
        try:
            text = str(args.DialogId)
            try:
                text += ": " + str(args.Message).replace("\r", " ").replace("\n", " ")[:300]
            except Exception:
                pass
            log("    REVIT IS SHOWING A DIALOG and waits for an answer - " + text)
            self.seen.append(text)
        except Exception:
            pass

    def __enter__(self):
        try:
            if self.app is not None:
                self.app.DialogBoxShowing += self._showing
        except Exception:
            self.app = None
        return self

    def __exit__(self, *exc):
        try:
            if self.app is not None:
                self.app.DialogBoxShowing -= self._showing
        except Exception:
            pass
        for text in self.seen[:5]:
            self.results["warnings"].append({"name": "Revit dialog during DWG export", "warning": text})
        return False


# Revit's own DWG export API has no property for this -- native Revit DWG
# export always writes raster images as external references. "Bind images
# as OLE" is done post-export instead, by driving full AutoCAD through COM
# (dwg_ole_binder.py) to open each freshly-written DWG, convert every
# raster image it finds to an embedded OLE object, detach the now-unused
# image references, and save. This requires full AutoCAD (not LT) installed
# and licensed on the machine running the export. The import is deferred
# into _ole_binder() so a machine without AutoCAD can still export DWGs
# normally with the checkbox left unchecked.
_ole_binder_module = None
_ole_binder_import_error = None


def _ole_binder():
    """Import dwg_ole_binder.py lazily and cache the result (or the import
    error) so we only pay/report the cost once per export run."""
    global _ole_binder_module, _ole_binder_import_error
    if _ole_binder_module is None and _ole_binder_import_error is None:
        try:
            import dwg_ole_binder
            dwg_ole_binder.LOG = log                      # its steps go to dwg_log.txt as well
            _ole_binder_module = dwg_ole_binder
        except Exception as error:
            _ole_binder_import_error = str(error)
    return _ole_binder_module, _ole_binder_import_error


def _bind_images_as_ole(dwg_path):
    """Run dwg_ole_binder.process_dwg_to_ole() on one freshly-exported DWG.

    Returns (warning_text_or_None, converted_count, total_count). A
    warning is only returned when something is actually worth flagging --
    a clean file with zero images (skipped_scan) is not a warning.
    """
    binder, import_error = _ole_binder()
    if binder is None:
        return (
            "AutoCAD automation is unavailable ({}). Install full AutoCAD "
            "(not LT) to use Bind Images as OLE; images stay linked as "
            "external references.".format(import_error),
            0, 0
        )

    try:
        result = binder.process_dwg_to_ole(dwg_path)
    except Exception as error:
        return ("OLE bind failed: {}".format(error), 0, 0)

    converted = result.get("converted", 0)
    total = result.get("total", 0)

    if result.get("skipped_scan") or (result.get("success") and total == 0):
        return (None, converted, total)

    if result.get("timed_out"):
        return (
            "Image scan timed out before converting anything; DWG left "
            "with images as external references. {}".format(
                "; ".join(result.get("warnings", []))
            ).rstrip("; "),
            converted, total
        )

    if not result.get("success"):
        messages = result.get("warnings", []) + result.get("failed", [])
        return (
            "{} of {} images bound as OLE; {}".format(
                converted, total, "; ".join(messages) or "see log for details"
            ),
            converted, total
        )

    return (None, converted, total)


# Shown in the setup drop-down when the model has no saved DWG setups.
# FIX: the old code raised "Select a DWG Export Setup" in that case and --
# because the exception escaped export_items() -- also threw away the
# results of a PDF export that had already finished in the same run.
DEFAULT_SETUP = "<Revit default DWG settings>"


def get_setup_names(doc):
    """Return all saved Revit DWG Export Setup names, sorted for a ComboBox.
    Falls back to [DEFAULT_SETUP] when the model has none."""
    names = []
    settings = FilteredElementCollector(doc).OfClass(ExportDWGSettings).ToElements()
    for setting in settings:
        try:
            if setting.Name:
                names.append(setting.Name)
        except Exception:
            pass
    names = sorted(set(names), key=lambda value: value.lower())
    return names or [DEFAULT_SETUP]


def get_options(doc, setup_name):
    """Load the exact predefined DWG settings selected by the user."""
    if not setup_name:
        raise ValueError("Select a DWG Export Setup before exporting.")
    if setup_name == DEFAULT_SETUP:
        return DWGExportOptions()

    try:
        return DWGExportOptions.GetPredefinedOptions(doc, str(setup_name))
    except Exception as error:
        raise ValueError(
            "Could not load DWG Export Setup '{}': {}".format(setup_name, error)
        )


def _element_from_item(item):
    """Accept either an Export Manager Item object or a raw Revit element."""
    try:
        return item.Element
    except Exception:
        return item


def _can_export(element):
    try:
        return bool(element.CanBePrinted)
    except Exception:
        # ViewSheet and normal printable views should expose CanBePrinted.
        return False


def _safe_stem(value, fallback="Export"):
    text = str(value or "").strip()
    for character in '<>:"/\\|?*':
        text = text.replace(character, "-")
    text = text.rstrip(". ")
    return text or fallback


def _unique_stem(folder, stem, reserved):
    """Keep filenames unique only *within this export run* -- if two
    selected items would produce the same name, the second one gets a
    '-2' suffix so neither export is lost. A pre-existing file left over
    from an earlier export run is intentionally overwritten, not avoided,
    so re-running the export keeps the folder in sync with the current
    selection."""
    candidate = stem
    index = 2
    while candidate.lower() in reserved:
        candidate = "{}-{}".format(stem, index)
        index += 1
    reserved.add(candidate.lower())
    return candidate


def _confirm_dwg(folder, stem, dwg_path, started):
    """True when the exported DWG is at dwg_path.

    If Revit wrote it under another name that still begins with our stem
    (older builds append the view name, e.g. 'stem-Sheet - E-101 - Plan.dwg'),
    and exactly one such file was written by this export, rename it to the
    expected name so the summary, the OLE step and the next run all agree.
    """
    if os.path.isfile(dwg_path):
        return True

    candidates = []
    prefix = stem.lower()
    try:
        for filename in os.listdir(folder):
            lowered = filename.lower()
            if not lowered.endswith(".dwg") or not lowered.startswith(prefix):
                continue
            path = os.path.join(folder, filename)
            try:
                # 2 s of slack for coarse file-system timestamps.
                if os.path.getmtime(path) >= started - 2:
                    candidates.append(path)
            except OSError:
                pass
    except OSError:
        return False

    if len(candidates) != 1:
        return False
    try:
        os.rename(candidates[0], dwg_path)
    except OSError:
        return False
    return os.path.isfile(dwg_path)


def _call_progress(callback, current, total, message):
    if callback is None:
        return
    try:
        callback(current, total, message)
    except helpers.ExportCancelled:
        raise
    except Exception:
        pass


def export_items(doc, items, output_folder, setup_name, filename_function,
                 progress_callback=None, bind_images_as_ole=False,
                 export_range="current", uidoc=None, active_view=None):
    """Export selected sheets/views one-by-one to uniquely named DWG files.

    Args:
        doc: Active Revit document.
        items: Export Manager item objects (with .Element) or raw view/sheet
            elements.
        output_folder: Existing or new target folder.
        setup_name: Name of a saved Revit DWG Export Setup.
        filename_function: Callable that accepts the original item and returns
            a filename stem without an extension.
        progress_callback: Optional callback(current, total, message).
        bind_images_as_ole: After each DWG is written, drive full AutoCAD
            via COM (dwg_ole_binder.py) to convert every raster image it
            contains into an embedded OLE object and re-save. Requires full
            AutoCAD (not LT) installed and licensed. Per-file problems are
            added to results["warnings"] rather than failing the export --
            the DWG itself was already written successfully by Revit.
        export_range: "current" (default, export each view's full crop
            region) or "visible" (temporarily crop whichever selected item
            is also the active, currently-open Revit view to match what is
            on screen, export it, then restore the original crop).
        uidoc / active_view: Needed only when export_range == "visible".

    Returns:
        dict with exported, skipped, errors and warnings lists.  Each
        exported entry has element_id, name and path fields, plus
        ole_converted/ole_total when bind_images_as_ole was requested.
    """
    if not output_folder:
        raise ValueError("Select a DWG output folder.")
    if not os.path.exists(output_folder):
        os.makedirs(output_folder)

    results = {"exported": [], "skipped": [], "errors": [], "warnings": []}
    try:
        options = get_options(doc, setup_name)
    except Exception as error:
        results["errors"].append({"name": "DWG Export Setup", "error": str(error)})
        return results
    try:                                                    # what the chosen export setup will do, for the log
        log("setup: links as external references (MergedViews off): {0}; shared coordinates: {1}; file version: {2}".format(
            not options.MergedViews, options.SharedCoords, options.FileVersion))
    except Exception:
        pass

    reserved_names = set()
    source_items = list(items or [])
    total = len(source_items)
    ole_used = False
    log("DWG run: {0} item(s), setup '{1}', bind images as OLE: {2}, folder: {3}".format(
        total, setup_name, bool(bind_images_as_ole), output_folder))

    try:
        for index, item in enumerate(source_items):
            element = _element_from_item(item)
            label = "Unknown View"
            try:
                label = element.SheetNumber
            except Exception:
                try:
                    label = element.Name
                except Exception:
                    pass

            _call_progress(progress_callback, index + 1, total, "Exporting DWG: {}".format(label))

            if element is None or not _can_export(element):
                results["skipped"].append({
                    "name": label,
                    "reason": "The selected item is not a printable sheet or view."
                })
                continue

            restore_crop = helpers.maybe_apply_visible_crop(
                doc, uidoc, element, export_range, active_view
            )

            try:
                stem = _safe_stem(filename_function(item), label)
                stem = _unique_stem(output_folder, stem, reserved_names)

                ids = List[ElementId]()
                ids.Add(element.Id)

                dwg_path = os.path.join(output_folder, stem + ".dwg")
                started = time.time()

                # FIX: pass the name WITH its ".dwg" extension.  Given a
                # bare stem, Revit treats whatever follows the last "." as
                # the extension, so a sheet number such as "E-1.01" could be
                # written as "E-1.dwg" -- the wrong name, and every sheet
                # "E-1.xx" then overwrote the same file.  The API documents
                # that ".dwg" is only appended when it is missing.
                log("--- {0}: Revit starts writing {1}.dwg".format(label, stem))
                with _DialogWatch(uidoc, results):
                    success = doc.Export(output_folder, stem + ".dwg", ids, options)
                log("    Revit finished after {0:.0f} s, success: {1}".format(time.time() - started, success))
                if not success:
                    raise RuntimeError("Revit returned False from DWG export.")

                on_disk = _confirm_dwg(output_folder, stem, dwg_path, started)
                entry = {
                    "element_id": helpers.element_id_value(element.Id),
                    "name": stem,
                    "path": dwg_path
                }
                # Record the DWG as exported BEFORE the (slow, cancellable)
                # OLE step, so a Cancel there still reports the file.
                results["exported"].append(entry)

                if not on_disk:
                    results["warnings"].append({
                        "name": stem,
                        "warning": "Revit reported success, but '{}.dwg' was "
                                   "not found in the output folder. Check the "
                                   "folder for a differently named file."
                                   .format(stem)
                    })
                elif bind_images_as_ole:
                    ole_used = True
                    _call_progress(
                        progress_callback, index + 1, total,
                        "Binding images as OLE: {}".format(label)
                    )
                    log("    binding images as OLE in AutoCAD ...")
                    bound = time.time()
                    warning, converted, ole_total = _bind_images_as_ole(dwg_path)
                    log("    AutoCAD finished after {0:.0f} s: {1} of {2} image(s) bound{3}".format(
                        time.time() - bound, converted, ole_total, "; " + str(warning).splitlines()[0] if warning else ""))
                    entry["ole_converted"] = converted
                    entry["ole_total"] = ole_total
                    if warning:
                        results["warnings"].append({"name": stem, "warning": warning})

            except helpers.ExportCancelled:
                log("    cancelled by the user")
                raise
            except Exception as error:
                log("    ERROR: {0}".format(str(error).splitlines()[0] if str(error) else error))
                results["errors"].append({
                    "name": label,
                    "error": str(error)
                })
            finally:
                if restore_crop is not None:
                    restore_crop()
    except helpers.ExportCancelled as stop:
        stop.partial = results
        raise
    finally:
        if ole_used:
            binder, _ = _ole_binder()
            if binder is not None:
                try:
                    binder.shutdown_autocad()
                except Exception:
                    pass

    return results


def summary_text(results):
    """Create a concise human-readable export summary for the UI."""
    exported = len(results.get("exported", []))
    skipped = len(results.get("skipped", []))
    errors = len(results.get("errors", []))
    lines = ["DWG exported: {}".format(exported)]
    if skipped:
        lines.append("Skipped: {}".format(skipped))

    ole_converted = sum(e.get("ole_converted", 0) for e in results.get("exported", []))
    ole_total = sum(e.get("ole_total", 0) for e in results.get("exported", []))
    if ole_total or any("ole_converted" in e for e in results.get("exported", [])):
        lines.append("Images bound as OLE: {} of {}".format(ole_converted, ole_total))

    if errors:
        lines.append("Errors: {}".format(errors))
        for entry in results.get("errors", []):
            lines.append("- {}: {}".format(entry["name"], entry["error"]))
    for entry in results.get("warnings", []):
        lines.append("Warning - {}: {}".format(entry["name"], entry["warning"]))
    return "\n".join(lines)
