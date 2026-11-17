# -*- coding: utf-8 -*-
"""Built-in Revit PDF export engine for the pyRevit Export Manager.

Supports one PDF per selected sheet/view and one combined PDF. Every export
explicitly calls PDFExportOptions.SetExportInBackground(False) so each
doc.Export() call finishes writing before the loop moves to the next item --
relying on Revit's default background export in a batch loop is a known
cause of Revit hanging or closing partway through a multi-item export.

For separate files, Revit can ignore PDFExportOptions.FileName and apply its
own PDF naming rule.  This module exports one item at a time, then renames the
new PDF to the filename produced by filename_builder.py.
"""

import os
import time

from Autodesk.Revit.DB import ElementId, PDFExportOptions
from System.Collections.Generic import List

import helpers


def _element_from_item(item):
    """Accept an Export Manager item object or a raw Revit element."""
    try:
        return item.Element
    except Exception:
        return item


def _label(element):
    try:
        return element.SheetNumber
    except Exception:
        try:
            return element.Name
        except Exception:
            return "Unknown View"


def _is_printable(element):
    try:
        return bool(element.CanBePrinted)
    except Exception:
        return False


def _safe_stem(value, fallback="Export"):
    result = str(value or "").strip()
    for character in '<>:"/\\|?*':
        result = result.replace(character, "-")
    result = result.rstrip(". ")
    return result or fallback


def _unique_stem(folder, stem, reserved):
    """Keep filenames unique only *within this export run* -- if two
    selected items would produce the same name, the second one gets a
    '-2' suffix so neither export is lost. A pre-existing file left over
    from an earlier export run is intentionally overwritten, not avoided,
    so re-running the export keeps the folder in sync with the current
    selection."""
    candidate = stem
    suffix = 2
    while candidate.lower() in reserved:
        candidate = "{}-{}".format(stem, suffix)
        suffix += 1
    reserved.add(candidate.lower())
    return candidate


def _pdf_snapshot(folder):
    """Return PDF path -> modification time for file-detection after export."""
    snapshot = {}
    try:
        for filename in os.listdir(folder):
            if filename.lower().endswith(".pdf"):
                path = os.path.join(folder, filename)
                try:
                    snapshot[path] = os.path.getmtime(path)
                except OSError:
                    pass
    except OSError:
        pass
    return snapshot


def _wait_for_exported_pdf(folder, before, timeout_seconds=20):
    """Find the PDF created/updated by the immediately preceding export."""
    end_time = time.time() + timeout_seconds
    newest = None

    while time.time() <= end_time:
        after = _pdf_snapshot(folder)
        candidates = []
        for path, modified in after.items():
            if path not in before or modified > before.get(path, 0):
                candidates.append((modified, path))

        if candidates:
            candidates.sort(reverse=True)
            newest = candidates[0][1]
            # Give Revit a brief moment to release its write handle.
            time.sleep(0.2)
            return newest

        time.sleep(0.2)

    return newest


def _move_into_place(created_path, target_path):
    """Rename the PDF Revit just wrote to the name the user asked for."""
    if os.path.normcase(created_path) == os.path.normcase(target_path):
        return
    try:
        if os.path.exists(target_path):
            # os.rename() raises on Windows if the destination already
            # exists -- remove the stale file from an earlier export run
            # first so this run overwrites it.
            os.remove(target_path)
        os.rename(created_path, target_path)
    except OSError as error:
        raise RuntimeError(
            "PDF was exported as '{}' but could not be renamed to '{}' "
            "(is the old file open in a PDF viewer?): {}".format(
                os.path.basename(created_path),
                os.path.basename(target_path),
                error
            )
        )


def _new_options(options_factory=None):
    """Create a fresh PDFExportOptions object for every Export call.

    Revit's PDF exporter defaults to running in the background on recent
    versions. Firing doc.Export() repeatedly in a loop (one call per view)
    before the previous background export has actually finished writing can
    destabilize Revit -- this is a known cause of Revit freezing or closing
    during batch PDF export. Forcing SetExportInBackground(False) makes each
    call block until that single export is fully done before the loop moves
    on to the next item.
    """
    if options_factory is not None:
        options = options_factory()
    else:
        options = PDFExportOptions()

    try:
        options.SetExportInBackground(False)
    except Exception:
        # Older API surfaces may not expose this method; safe to continue,
        # the polling wait below still guards against a false-positive file.
        pass

    return options


def _call_progress(callback, current, total, message):
    """Report progress.  FIX: the old version swallowed *every* exception,
    including the one the progress bar raises on Cancel, so Cancel never
    stopped a batch.  ExportCancelled is now allowed through."""
    if callback is None:
        return
    try:
        callback(current, total, message)
    except helpers.ExportCancelled:
        raise
    except Exception:
        pass


def _prepare_folder(folder):
    if not folder:
        raise ValueError("Select a PDF output folder.")
    if not os.path.exists(folder):
        os.makedirs(folder)


def export_separate(doc, items, output_folder, filename_function,
                    options_factory=None, progress_callback=None,
                    export_range="current", uidoc=None, active_view=None):
    """Export one PDF for every printable selected item.

    Args:
        doc: Active Revit document.
        items: Export Manager item objects or raw sheets/views.
        output_folder: Target folder.
        filename_function: Callable(item) -> filename stem, no .pdf extension.
        options_factory: Optional callable returning a configured
            PDFExportOptions instance. It must return a new object per call.
        progress_callback: Optional callback(current, total, message).
        export_range: "current" (default, export each view's full crop
            region) or "visible" (temporarily crop whichever selected item
            is also the active, currently-open Revit view to match what is
            on screen, export it, then restore the original crop).
        uidoc / active_view: Needed only when export_range == "visible".

    Returns a dict with exported, skipped, errors and warnings lists.
    """
    _prepare_folder(output_folder)
    source_items = list(items or [])
    results = {"exported": [], "skipped": [], "errors": [], "warnings": []}
    reserved_names = set()

    try:
        _export_separate_loop(
            doc, source_items, output_folder, filename_function,
            options_factory, progress_callback, export_range, uidoc,
            active_view, results, reserved_names
        )
    except helpers.ExportCancelled as stop:
        stop.partial = results
        raise
    return results


def _export_separate_loop(doc, source_items, output_folder, filename_function,
                          options_factory, progress_callback, export_range,
                          uidoc, active_view, results, reserved_names):
    total = len(source_items)
    for index, item in enumerate(source_items):
        element = _element_from_item(item)
        label = _label(element)
        _call_progress(progress_callback, index + 1, total, "Exporting PDF: {}".format(label))

        if element is None or not _is_printable(element):
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
            target_path = os.path.join(output_folder, stem + ".pdf")

            ids = List[ElementId]()
            ids.Add(element.Id)

            options = _new_options(options_factory)
            options.Combine = False

            # FileName is used by some Revit builds.  Other builds ignore it
            # for separate PDFs; the rename below handles both behaviours.
            try:
                options.FileName = stem
            except Exception:
                pass

            before = _pdf_snapshot(output_folder)
            success = doc.Export(output_folder, ids, options)
            if not success:
                raise RuntimeError("Revit returned False from PDF export.")

            created_path = _wait_for_exported_pdf(output_folder, before)
            if created_path is None:
                raise RuntimeError("Revit did not create a detectable PDF file.")

            _move_into_place(created_path, target_path)

            results["exported"].append({
                "element_id": helpers.element_id_value(element.Id),
                "name": stem,
                "path": target_path
            })

        except helpers.ExportCancelled:
            raise
        except Exception as error:
            results["errors"].append({"name": label, "error": str(error)})
        finally:
            if restore_crop is not None:
                restore_crop()


def export_combined(doc, items, output_folder, package_name,
                    options_factory=None, progress_callback=None,
                    export_range="current", uidoc=None, active_view=None):
    """Export all printable selected items into a single PDF package.

    If export_range == "visible" and the active Revit view is among the
    selected items, that one view's on-screen crop is applied for the
    duration of this export and restored afterward; other pages in the
    combined set are unaffected.
    """
    _prepare_folder(output_folder)
    results = {"exported": [], "skipped": [], "errors": [], "warnings": []}
    source_items = list(items or [])
    ids = List[ElementId]()
    restore_crop = None

    try:
        for index, item in enumerate(source_items):
            element = _element_from_item(item)
            label = _label(element)
            _call_progress(
                progress_callback, index + 1, len(source_items),
                "Adding to combined PDF: {}".format(label)
            )

            if element is None or not _is_printable(element):
                results["skipped"].append({
                    "name": label,
                    "reason": "The selected item is not a printable sheet or view."
                })
                continue
            ids.Add(element.Id)

            if restore_crop is None:
                restore_crop = helpers.maybe_apply_visible_crop(
                    doc, uidoc, element, export_range, active_view
                )

        if ids.Count == 0:
            results["errors"].append({
                "name": "Combined PDF",
                "error": "No printable sheets or views were selected."
            })
            return results

        try:
            stem = _safe_stem(package_name, "Combined Set")
            target_path = os.path.join(output_folder, stem + ".pdf")
            options = _new_options(options_factory)
            options.Combine = True
            options.FileName = stem  # Do not append .pdf; Revit adds it.

            before = _pdf_snapshot(output_folder)
            success = doc.Export(output_folder, ids, options)
            if not success:
                raise RuntimeError("Revit returned False from combined PDF export.")

            # FIX: the combined file was reported as exported without ever
            # checking it exists under the requested name.  Detect what
            # Revit actually wrote and move it into place, exactly like the
            # separate-file path does.
            created_path = _wait_for_exported_pdf(output_folder, before)
            if created_path is not None:
                _move_into_place(created_path, target_path)
            elif not os.path.isfile(target_path):
                raise RuntimeError("Revit did not create a detectable PDF file.")

            results["exported"].append({
                "element_id": None,
                "name": stem,
                "path": target_path
            })
        except Exception as error:
            results["errors"].append({"name": "Combined PDF", "error": str(error)})

    except helpers.ExportCancelled as stop:
        stop.partial = results
        raise
    finally:
        # FIX: previously a Cancel during the gather loop left the
        # temporary crop applied to the user's view.
        if restore_crop is not None:
            restore_crop()

    return results


def summary_text(results):
    """Create concise text for an Export Manager completion dialog."""
    exported = len(results.get("exported", []))
    skipped = len(results.get("skipped", []))
    errors = len(results.get("errors", []))
    lines = ["PDF exported: {}".format(exported)]
    if skipped:
        lines.append("Skipped: {}".format(skipped))
    if errors:
        lines.append("Errors: {}".format(errors))
        for entry in results.get("errors", []):
            lines.append("- {}: {}".format(entry["name"], entry["error"]))
    for entry in results.get("warnings", []):
        lines.append("Warning - {}: {}".format(entry["name"], entry["warning"]))
    return "\n".join(lines)
