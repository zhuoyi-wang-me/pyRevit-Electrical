# -*- coding: utf-8 -*-
"""PDF through a PDF PRINTER (the way DiRoots ProSheets does it).

export_pdf.py writes PDFs with Revit's own PDF export. This module instead
PRINTS each sheet / view with Revit's print engine to a PDF printer, so the
result - line weights included - is what that printer produces.

    PDF24                    the printer is switched to "save automatically"
                             into a temporary folder for the duration of the
                             run; every PDF is then moved to its final name.
                             PDF24's own settings are put back afterwards.
    Microsoft Print to PDF   printed "to file" straight to the final name.
    other printers           printed "to file" as well; a printer that insists
                             on asking for a file name cannot be automated.

What is changed, and put back:
  * Revit: a temporary print setup is saved inside a transaction that is
    ROLLED BACK after each print, so the model keeps no trace of it.
  * PDF24: the values under HKEY_CURRENT_USER\\Software\\PDF24\\Services\\PDF
    (Handler, AutoSave...) are remembered before the run and restored after
    it, also when the run is cancelled or fails.

The paper size is taken from the sheet (title block size) and matched to a
paper size of the printer. A size the printer does not offer is printed on
the next larger one and reported as a warning.
"""
import json
import os
import time

from Autodesk.Revit.DB import (
    BuiltInParameter, FamilyInstance, FilteredElementCollector, Transaction, ViewSet, ViewSheet
)
import Autodesk.Revit.DB as DB

import helpers

PDF24 = "PDF24"
PDF24_KEY = r"Software\PDF24\Services\PDF"
TEMP_FOLDER = "_export_manager_print"
WAIT_SECONDS = 600                      # per sheet, for the printer to deliver its PDF
LOG_PATH = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "EleTools",
                        "ExportManager", "print_log.txt")
# PDF24's own settings as they were before a run changed them. The file exists only while a
# run is going on; if Revit is closed in the middle, the next run puts them back from here.
PDF24_BEFORE = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "EleTools",
                            "ExportManager", "pdf24_before.json")


def log(text, restart=False):
    """One line in print_log.txt, written at once: what was asked of Revit's
    print engine for each sheet and what Revit reported back."""
    try:
        folder = os.path.dirname(LOG_PATH)
        if not os.path.isdir(folder):
            os.makedirs(folder)
        if not restart and os.path.isfile(LOG_PATH) and os.path.getsize(LOG_PATH) > 400000:
            restart = True                                  # several Revit sessions write here: start again when it gets long
        with open(LOG_PATH, "w" if restart else "a") as handle:
            handle.write("{0}  [Revit {1}]  {2}\n".format(time.strftime("%H:%M:%S"), os.getpid(), text))
    except Exception:
        pass
FEET_TO_MM = 304.8

# name fragments of the paper sizes a printer may offer -> (short, long) in mm
STANDARD_PAPERS = [
    ("A4", 210, 297), ("A3", 297, 420), ("A2", 420, 594), ("A1", 594, 841), ("A0", 841, 1189),
    ("ARCH A", 229, 305), ("ARCH B", 305, 457), ("ARCH C", 457, 610), ("ARCH D", 610, 914),
    ("ARCH E", 914, 1219), ("ARCH E1", 762, 1067),
    ("Letter", 216, 279), ("Legal", 216, 356), ("Tabloid", 279, 432), ("Ledger", 279, 432),
    ("ANSI C", 432, 559), ("ANSI D", 559, 864), ("ANSI E", 864, 1118),
]
SIZE_TOLERANCE_MM = 6.0


# ---------------------------------------------------------------------------
# printers
# ---------------------------------------------------------------------------
def installed_printers():
    """Names of the printers on this computer, PDF printers first."""
    names = []
    try:
        import clr
        clr.AddReference("System.Drawing")
        from System.Drawing.Printing import PrinterSettings
        names = [str(name) for name in PrinterSettings.InstalledPrinters]
    except Exception:
        pass
    return sorted(names, key=lambda name: ("pdf" not in name.lower(), name.lower()))


def is_pdf24(printer):
    return (printer or "").strip().lower() == PDF24.lower()


# ---------------------------------------------------------------------------
# PDF24: save automatically, without asking, into our folder
# ---------------------------------------------------------------------------
class _Pdf24Turn(object):
    """with _Pdf24Turn(waiting): ... -> only ONE Revit at a time prints through PDF24.

    Why: PDF24 has a single "save automatically into this folder" setting for the
    whole Windows user. When two Revit sessions exported at the same time, the
    second one pointed PDF24 at its own folder while the first was still printing:
    the PDFs of the first landed in the folder of the second (which took them for
    its own sheets, or deleted them as leftovers), and whichever finished first
    switched "save automatically" off under the other. The result was missing,
    swapped or empty PDFs.

    A second Revit now waits here until the first has finished its run.
    waiting(): called about twice a second while waiting; it may raise
    helpers.ExportCancelled to stop waiting."""

    NAME = "EleTools.ExportManager.PDF24"

    def __init__(self, waiting=None):
        self.waiting = waiting
        self.mutex = None
        self.waited = 0.0

    def __enter__(self):
        from System.Threading import Mutex, AbandonedMutexException
        mutex = Mutex(False, self.NAME)
        started = time.time()
        try:
            while True:
                try:
                    got = mutex.WaitOne(500)
                except AbandonedMutexException:
                    got = True                              # the other Revit was closed while printing: our turn
                if got:
                    break
                if self.waiting is not None:
                    self.waiting()
        except Exception:
            mutex.Dispose()
            raise
        self.mutex = mutex
        self.waited = time.time() - started
        if self.waited > 1.0:
            log("waited {0:.0f} s for another Revit to finish printing through PDF24".format(self.waited))
        return self

    def __exit__(self, *exc):
        try:
            self.mutex.ReleaseMutex()
            self.mutex.Dispose()
        except Exception:
            pass
        return False


class _Pdf24AutoSave(object):
    """with _Pdf24AutoSave(folder): ... -> PDF24 saves every print job into
    `folder` as <job name>.pdf without a dialog; on leaving, the user's own
    PDF24 settings are back exactly as they were."""

    WANTED = [("Handler", "autoSave"), ("AutoSaveFilename", "$fileName"), ("AutoSaveOpenDir", 0),
              ("AutoSaveShowProgress", 0), ("AutoSaveUseFileCmd", 0), ("AutoSaveOverwriteFile", 1),
              ("AutoSaveUseFileChooser", 0)]

    def __init__(self, folder):
        self.folder = folder
        self.saved = None

    @staticmethod
    def _registry():
        """The .NET Registry class.

        FIX: in Revit 2025 and later (.NET 8) the registry classes are no
        longer part of the core library, so 'from Microsoft.Win32 import ...'
        failed with "Cannot import name RegistryValueKind" until their own
        assembly is referenced. RegistryValueKind is not needed at all: a
        whole number is stored as a DWORD and text as a string by default,
        which is exactly what PDF24 uses."""
        import clr
        for assembly in ("Microsoft.Win32.Registry", "mscorlib"):
            try:
                clr.AddReference(assembly)
            except Exception:
                pass
            try:
                from Microsoft.Win32 import Registry
                return Registry
            except Exception:
                continue
        raise RuntimeError("Windows' registry cannot be reached from this Revit / pyRevit, so PDF24 "
                           "cannot be switched to save automatically. Use 'Revit PDF export' or "
                           "'Microsoft Print to PDF' instead.")

    def _key(self, writable):
        registry = self._registry()
        key = registry.CurrentUser.OpenSubKey(PDF24_KEY, writable)
        if key is None and writable:
            key = registry.CurrentUser.CreateSubKey(PDF24_KEY)
        return key

    def __enter__(self):
        import System
        key = self._key(True)
        if key is None:
            raise RuntimeError("PDF24's settings could not be opened. Is PDF24 Creator installed for this user?")
        try:
            wanted = self.WANTED + [("AutoSaveDir", self.folder)]
            # (name, value as it was, None) - the value keeps its own .NET
            # type (Int32 / String), so writing it back restores the same kind
            self.saved = [(name, key.GetValue(name), None) for name, _value in wanted]
            left_behind = _read_before()
            if left_behind is not None:
                # an earlier run never put PDF24's settings back (Revit was closed in the middle):
                # what is in the registry now is OUR setting, the user's own is in the file
                self.saved = [(name, left_behind.get(name), None) for name, _value in wanted]
                log("PDF24 settings of an interrupted run are put back at the end of this one")
            else:
                _write_before(dict((name, _plain(value)) for name, value, _kind in self.saved))
            for name, value in wanted:
                key.SetValue(name, System.Int32(value) if isinstance(value, int) else value)
        finally:
            key.Close()
        return self

    def __exit__(self, *exc):
        if self.saved is None:
            return False
        try:
            key = self._key(True)
            try:
                for name, value, kind in self.saved:
                    if value is None:
                        try:
                            key.DeleteValue(name, False)
                        except Exception:
                            pass
                    else:
                        import System
                        key.SetValue(name, System.Int32(value) if isinstance(value, int) and not isinstance(value, bool) else value)
            finally:
                key.Close()
            _forget_before()
        except Exception:
            pass
        return False


def _plain(value):
    """A registry value as plain Python (whole number or text), for the file."""
    if value is None:
        return None
    try:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, (int, long)):
            return int(value)
    except NameError:
        if isinstance(value, int):
            return int(value)
    text = u"{0}".format(value)
    try:
        if type(value).__name__ in ("Int32", "Int64", "UInt32"):
            return int(text)
    except Exception:
        pass
    return text


def _read_before():
    try:
        with open(PDF24_BEFORE) as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _write_before(values):
    try:
        folder = os.path.dirname(PDF24_BEFORE)
        if not os.path.isdir(folder):
            os.makedirs(folder)
        with open(PDF24_BEFORE, "w") as handle:
            json.dump(values, handle)
    except Exception:
        pass


def _forget_before():
    try:
        if os.path.isfile(PDF24_BEFORE):
            os.remove(PDF24_BEFORE)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# paper size
# ---------------------------------------------------------------------------
def sheet_size_mm(doc, view):
    """(width, height) in mm of a sheet, from its title block; None for a
    view or a sheet without a title block."""
    if not isinstance(view, ViewSheet):
        return None
    best = None
    try:
        blocks = (FilteredElementCollector(doc, view.Id).OfCategory(DB.BuiltInCategory.OST_TitleBlocks)
                  .OfClass(FamilyInstance))
        for block in blocks:
            try:
                width = block.get_Parameter(BuiltInParameter.SHEET_WIDTH).AsDouble() * FEET_TO_MM
                height = block.get_Parameter(BuiltInParameter.SHEET_HEIGHT).AsDouble() * FEET_TO_MM
            except Exception:
                continue
            if width > 0 and height > 0 and (best is None or width * height > best[0] * best[1]):
                best = (width, height)
    except Exception:
        pass
    if best is None:
        try:
            outline = view.Outline
            best = ((outline.Max.U - outline.Min.U) * FEET_TO_MM, (outline.Max.V - outline.Min.V) * FEET_TO_MM)
        except Exception:
            return None
    return best


def _paper_names(short, long_):
    """Standard paper names for a size, exact match first then the next larger ones."""
    exact = [name for name, a, b in STANDARD_PAPERS
             if abs(a - short) <= SIZE_TOLERANCE_MM and abs(b - long_) <= SIZE_TOLERANCE_MM]
    larger = [name for name, a, b in sorted(STANDARD_PAPERS, key=lambda item: item[1] * item[2])
              if a + SIZE_TOLERANCE_MM >= short and b + SIZE_TOLERANCE_MM >= long_ and name not in exact]
    return exact, larger


def _find_paper(paper_sizes, name):
    """The printer's paper size called `name` ('A1' also finds 'ISO A1', not 'A10')."""
    wanted = name.lower()
    candidates = []
    for paper in paper_sizes:
        try:
            label = paper.Name or ""
        except Exception:
            continue
        lowered = label.lower()
        words = lowered.replace("(", " ").replace(")", " ").replace("-", " ").replace("_", " ").split()
        if lowered == wanted:
            return paper
        if " " in wanted:
            if wanted in lowered and (wanted + "1") not in lowered:
                candidates.append((len(label), paper))
        elif wanted in words:
            candidates.append((len(label), paper))
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1] if candidates else None


def printer_papers(printer):
    """[(name, short side mm, long side mm)] - the paper sizes the printer
    really offers, with their real dimensions (from Windows)."""
    papers = []
    try:
        import clr
        clr.AddReference("System.Drawing")
        from System.Drawing.Printing import PrinterSettings
        settings = PrinterSettings()
        settings.PrinterName = printer
        for paper in settings.PaperSizes:
            a, b = paper.Width * 0.254, paper.Height * 0.254          # hundredths of an inch -> mm
            if a > 0 and b > 0:
                papers.append((str(paper.PaperName), min(a, b), max(a, b)))
    except Exception:
        pass
    return papers


def _paper_by_name(paper_sizes, name):
    for paper in paper_sizes:
        try:
            if (paper.Name or "").strip().lower() == name.strip().lower():
                return paper
        except Exception:
            continue
    return None


def choose_paper(paper_sizes, size_mm, printer=None):
    """(paper size or None, landscape?, note or None) for a sheet size.

    The printer's own list of paper sizes WITH dimensions is used first: the
    sheet goes on the paper of exactly its size, whatever that paper is
    called (also a custom one such as 'PS_1719x900'). Without that list the
    standard names (A0, A1, ARCH D ...) are tried."""
    if size_mm is None:
        return None, None, None
    width, height = float(size_mm[0]), float(size_mm[1])
    short, long_ = min(width, height), max(width, height)
    real = printer_papers(printer) if printer else []
    same = [item for item in real
            if abs(item[1] - short) <= SIZE_TOLERANCE_MM and abs(item[2] - long_) <= SIZE_TOLERANCE_MM]
    bigger = sorted([item for item in real
                     if item[1] + SIZE_TOLERANCE_MM >= short and item[2] + SIZE_TOLERANCE_MM >= long_
                     and item not in same], key=lambda item: item[1] * item[2])
    exact, larger = _paper_names(short, long_)
    # 1. a paper of exactly the sheet's size: by the printer's own list, then by its standard name
    for item in same:
        paper = _paper_by_name(paper_sizes, item[0])
        if paper is not None:
            return paper, width >= height, None
    for name in exact:
        paper = _find_paper(paper_sizes, name)
        if paper is not None:
            return paper, width >= height, None
    # 2. no such paper: the smallest one that holds the sheet
    for item in bigger:
        paper = _paper_by_name(paper_sizes, item[0])
        if paper is not None:
            return paper, width >= height, (
                "sheet is {0:.0f} x {1:.0f} mm; the printer has no paper of that size, printed on {2} "
                "({3:.0f} x {4:.0f} mm)".format(width, height, paper.Name, float(item[1]), float(item[2])))
    for name in larger:
        paper = _find_paper(paper_sizes, name)
        if paper is not None:
            return paper, width >= height, (
                "sheet is {0:.0f} x {1:.0f} mm; the printer has no such paper size, printed on {2}".format(
                    width, height, paper.Name))
    return None, width >= height, (
        "sheet is {0:.0f} x {1:.0f} mm; no fitting paper size on the printer, its current paper was used".format(
            width, height))


# ---------------------------------------------------------------------------
# Revit print setup
# ---------------------------------------------------------------------------
def _enum(type_name, member):
    enum_type = getattr(DB, type_name, None)
    return getattr(enum_type, member, None) if enum_type is not None else None


def _apply_parameters(parameters, settings, paper, landscape, problems):
    """Copy the Format-tab settings onto Revit's print parameters. A setting
    Revit refuses in the current mode is noted once, not fatal."""
    colour = {"Color": "Color", "Black Lines": "BlackLine", "GrayScale": "GrayScale"}.get(
        settings.get("color"), "Color")
    wanted = [
        ("PaperSize", paper),
        ("PageOrientation", None if landscape is None else _enum(
            "PageOrientationType", "Landscape" if landscape else "Portrait")),
        ("PaperPlacement", _enum("PaperPlacementType", "Center" if settings.get("placement_center", True)
                                 else "Margins")),
        ("MarginType", None if settings.get("placement_center", True) else _enum("MarginType", "NoMargin")),
        ("ZoomType", _enum("ZoomType", "FitToPage" if settings.get("fit_to_page") else "Zoom")),
        ("Zoom", None if settings.get("fit_to_page") else int(settings.get("zoom", 100))),
        ("HiddenLineViews", _enum("HiddenLineViewsType", "RasterProcessing" if settings.get("raster")
                                  else "VectorProcessing")),
        ("RasterQuality", _enum("RasterQualityType", settings.get("raster_quality", "High"))),
        ("ColorDepth", _enum("ColorDepthType", colour)),
        ("ViewLinksinBlue", bool(settings.get("links_blue")) if colour == "Color" else None),
        ("HideReforWorkPlanes", bool(settings.get("hide_refs", True))),
        ("HideUnreferencedViewTags", bool(settings.get("hide_unref_tags", True))),
        ("HideScopeBoxes", bool(settings.get("hide_scope", True))),
        ("HideCropBoundaries", bool(settings.get("hide_crop", True))),
        ("ReplaceHalftoneWithThinLines", bool(settings.get("halftone_thin"))),
        ("MaskCoincidentLines", bool(settings.get("mask_coincident"))),
    ]
    for name, value in wanted:
        if value is None:
            continue
        try:
            setattr(parameters, name, value)
        except Exception as error:
            problems.setdefault(name, str(error))


def _print_one(doc, view, printer, settings, to_file_path, problems, spool_path=None):
    """Print one view. Returns a note about the paper, or None.

    to_file_path: print "to file" under exactly this name (printers that
    write the file themselves, e.g. Microsoft Print to PDF).
    spool_path:   for PDF24. Revit calls PDF24 a "virtual" printer and then
    insists on a file name ("PrintToFile cannot be set to false while the
    printer is Virtual"). It is given this name in the temporary folder; the
    job still goes through PDF24, which saves the PDF there by itself.

    Everything Revit needs saved (the temporary print setup) lives in a
    transaction that is rolled back, so nothing stays in the model."""
    manager = doc.PrintManager
    manager.SelectNewPrintDriver(printer)
    manager = doc.PrintManager                     # the driver change gives a new object
    transaction = Transaction(doc, "Export Manager - print")
    transaction.Start()
    try:
        # FIX: the range must be "Current". With "Select", Revit printed the
        # whole stored sheet set (22 sheets, 1.2 GB of print data, 7 minutes)
        # for every single sheet asked for, and then reported the job as failed.
        manager.PrintRange = DB.PrintRange.Current
        log("print range: {0}".format(manager.PrintRange))
        manager.CombinedFile = True
        if to_file_path:
            manager.PrintToFile = True
            manager.PrintToFileName = to_file_path
        else:
            try:
                manager.PrintToFile = False
            except Exception:
                # FIX: a virtual printer must be given a file name
                manager.PrintToFile = True
                manager.PrintToFileName = spool_path
        setup = manager.PrintSetup
        setup.CurrentPrintSetting = setup.InSession
        size = sheet_size_mm(doc, view)
        if size is not None:
            size = (float(size[0]), float(size[1]))
        paper, landscape, note = choose_paper(manager.PaperSizes, size, printer)
        log("sheet size {0}; paper chosen: {1}; landscape: {2}".format(
            "unknown" if size is None else "{0:.0f} x {1:.0f} mm".format(size[0], size[1]),
            getattr(paper, "Name", None), landscape))
        # The parameters are set on the CURRENT setting (the in-session one)
        # and then saved under a name: only a saved setting is reliably used
        # by Revit. The first version set them on a separate copy of the
        # in-session parameters and the printer's default paper (Letter) was
        # used - one sheet came out cut into 21 pages.
        _apply_parameters(setup.CurrentPrintSetting.PrintParameters, settings, paper, landscape, problems)
        saved = False
        try:
            saved = bool(setup.SaveAs("Export Manager temp {0}".format(int(time.time() * 1000) % 100000000)))
        except Exception as error:
            log("SaveAs failed: {0}".format(error))
        manager.Apply()
        # what Revit will really use
        try:
            used = manager.PrintSetup.CurrentPrintSetting.PrintParameters
            used_paper = getattr(used.PaperSize, "Name", None)
            log("setup saved: {0}; Revit will use paper {1}, {2}, zoom {3} {4}, {5}".format(
                saved, used_paper, used.PageOrientation, used.ZoomType,
                used.Zoom if str(used.ZoomType).endswith("Zoom") else "", used.HiddenLineViews))
            if paper is not None and used_paper != paper.Name:
                raise RuntimeError(
                    "Revit did not accept the print setup (paper is '{0}', not '{1}'), so the sheet "
                    "was not printed.".format(used_paper, paper.Name))
        except RuntimeError:
            raise
        except Exception as error:
            log("could not read the setup back: {0}".format(error))
        if paper is None and size is not None:
            raise RuntimeError("No paper size of the printer fits this sheet ({0:.0f} x {1:.0f} mm), "
                               "so it was not printed.".format(size[0], size[1]))
        sent = manager.SubmitPrint(view)
        log("print job sent: {0}".format(sent))
        if not sent:
            raise RuntimeError("Revit did not send the print job.")
        return note
    finally:
        try:
            transaction.RollBack()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# waiting for the printer's file
# ---------------------------------------------------------------------------
def _pdfs(folder):
    found = {}
    try:
        for name in os.listdir(folder):
            if name.lower().endswith(".pdf"):
                path = os.path.join(folder, name)
                try:
                    found[path] = (os.path.getmtime(path), os.path.getsize(path))
                except OSError:
                    pass
    except OSError:
        pass
    return found


def _is_complete(path):
    """A PDF the printer has finished: it starts with %PDF, ends with %%EOF
    and can be opened. (A PostScript spool file also ends with %%EOF, so the
    start is checked too.)"""
    try:
        with open(path, "rb") as handle:
            if handle.read(4) != b"%PDF":
                return False
            handle.seek(0, 2)
            size = handle.tell()
            if size < 200:
                return False
            handle.seek(max(0, size - 1024))
            return b"%%EOF" in handle.read()
    except (IOError, OSError):
        return False


def _is_postscript(path):
    try:
        with open(path, "rb") as handle:
            return handle.read(4) == b"%!PS"
    except (IOError, OSError):
        return False


NAMELESS_GRACE = 45                     # s: a new PDF that names no sheet is only taken after this long


def _owner(path, sheets):
    """Which sheet a PDF made by PDF24 belongs to, from its file name; None when it names none of them.
    sheets: {sheet key: [names that identify it: the file stem asked for, the sheet number]} - the longest
    name found in the file name wins (5B16101 is not taken for 5B1610)."""
    base = os.path.splitext(os.path.basename(path))[0].lower()
    best, length = None, 0
    for key, names in sheets.items():
        for name in names:
            name = (name or "").lower()
            if name and name in base and len(name) > length:
                best, length = key, len(name)
    return best


def _wait_for_pdf(folder, before, cancelled, timeout=None, expect=None, sheets=None):
    """The PDF that appeared in `folder` since `before`, once it is complete.

    FIX: PDFs got the wrong drawing inside ("...5B16102.pdf" showed 5B16101). The first new PDF in the folder
    was taken for the sheet being printed - but when PDF24 delivers a sheet late, its PDF turns up while the
    NEXT sheet is waited for, and from there on every file is one sheet off. Now, with `expect` (the key of the
    sheet being printed) and `sheets` (every sheet of the run so far, see _owner): only the PDF whose name
    belongs to this sheet is taken; a late PDF of an earlier sheet is passed over; a PDF that names no sheet
    at all (another PDF24 name setting) is taken only after NAMELESS_GRACE seconds."""
    start = time.time()
    end = start + (WAIT_SECONDS if timeout is None else timeout)
    noted = set()
    while time.time() < end:
        if cancelled is not None:
            cancelled()
        nameless = []
        for path, stamp in sorted(_pdfs(folder).items()):
            if (path in before and stamp == before[path]) or not _is_complete(path):
                continue
            if expect is None or sheets is None:
                return path
            owner = _owner(path, sheets)
            if owner == expect:
                return path
            if owner is None:
                nameless.append(path)
            elif path not in noted:
                noted.add(path)
                log("a late PDF of an earlier sheet ({0}) turned up and was NOT taken for this one: {1}".format(owner, os.path.basename(path)))
        if nameless and time.time() - start > min(NAMELESS_GRACE, (end - start) / 2.0):
            log("the PDF names no sheet; taken for this one after {0:.0f} s: {1}".format(time.time() - start, os.path.basename(nameless[0])))
            return nameless[0]
        time.sleep(0.4)
    return None


def _move(source, target):
    if os.path.exists(target):
        os.remove(target)                          # this run replaces an older export
    os.rename(source, target)


# ---------------------------------------------------------------------------
# the export
# ---------------------------------------------------------------------------
def _label(element):
    try:
        return element.SheetNumber
    except Exception:
        try:
            return element.Name
        except Exception:
            return "Unknown View"


def _safe_stem(value, fallback="Export"):
    result = str(value or "").strip()
    for character in '<>:"/\\|?*':
        result = result.replace(character, "-")
    return result.rstrip(". ") or fallback


def export_separate(doc, items, output_folder, filename_function, settings, printer,
                    progress_callback=None, export_range="current", uidoc=None, active_view=None):
    """One PDF per sheet / view, printed through `printer`.

    Returns the same result dict as export_pdf.export_separate."""
    if not output_folder:
        raise ValueError("Select a PDF output folder.")
    if not os.path.exists(output_folder):
        os.makedirs(output_folder)
    results = {"exported": [], "skipped": [], "errors": [], "warnings": []}
    if printer not in installed_printers():
        results["errors"].append({"name": "PDF printer",
                                  "error": "The printer '{0}' is not installed on this computer.".format(printer)})
        return results

    source_items = list(items or [])
    reserved = set()
    known = {}                                   # sheet key -> names that identify its PDF (file stem, sheet number)
    problems = {}
    pdf24 = is_pdf24(printer)
    # a temporary folder of its own for every run: two Revit sessions exporting into the same
    # output folder never see (or delete) each other's files
    temp_root = os.path.join(output_folder, TEMP_FOLDER)
    temp = os.path.join(temp_root, "{0}-{1}".format(os.getpid(), int(time.time() * 1000) % 100000000))

    def report(index, message):
        if progress_callback is None:
            return
        try:
            progress_callback(index, len(source_items), message)
        except helpers.ExportCancelled:
            raise
        except Exception:
            pass

    def run():
        log("print run of Revit process {0}: {1} item(s) through {2}".format(os.getpid(), len(source_items), printer))
        for index, item in enumerate(source_items):
            try:
                element = item.Element
            except Exception:
                element = item
            label = _label(element)
            log("--- {0}".format(label))
            report(index + 1, "Printing to {0}: {1}".format(printer, label))
            try:
                printable = bool(element.CanBePrinted)
            except Exception:
                printable = False
            if element is None or not printable:
                results["skipped"].append({"name": label,
                                           "reason": "The selected item is not a printable sheet or view."})
                continue
            restore_crop = helpers.maybe_apply_visible_crop(doc, uidoc, element, export_range, active_view)
            try:
                stem = _safe_stem(filename_function(item), label)
                candidate, number = stem, 2
                while candidate.lower() in reserved:
                    candidate = "{0}-{1}".format(stem, number)
                    number += 1
                stem = candidate
                reserved.add(stem.lower())
                target = os.path.join(output_folder, stem + ".pdf")

                if pdf24:
                    before = _pdfs(temp)
                    spool = os.path.join(temp, stem + ".pdf")
                    key = stem.lower()
                    known[key] = [stem, str(label)]
                    note = _print_one(doc, element, printer, settings, None, problems, spool)
                    made = _wait_for_pdf(temp, before, lambda: report(index + 1, "Waiting for PDF24: {0}".format(label)),
                                         expect=key, sheets=known)
                    if made is None:
                        if _is_postscript(spool):
                            raise RuntimeError(
                                "Revit wrote the raw print data to a file instead of sending it to PDF24, so "
                                "no PDF was made. Use 'Microsoft Print to PDF' or 'Revit PDF export' for "
                                "this sheet and report this message.")
                        raise RuntimeError("PDF24 did not deliver a PDF within {0} s. Is the PDF24 backend "
                                           "running, and is a PDF24 window waiting for an answer?".format(WAIT_SECONDS))
                    _move(made, target)
                    # anything else this job left in the temporary folder
                    for leftover in os.listdir(temp):
                        path = os.path.join(temp, leftover)
                        if path not in before:
                            try:
                                os.remove(path)
                            except OSError:
                                pass
                else:
                    if os.path.exists(target):
                        os.remove(target)
                    note = _print_one(doc, element, printer, settings, target, problems)
                    end = time.time() + WAIT_SECONDS
                    while time.time() < end and not _is_complete(target):
                        report(index + 1, "Waiting for {0}: {1}".format(printer, label))
                        time.sleep(0.4)
                    if not _is_complete(target):
                        raise RuntimeError("'{0}' did not write the PDF. Some printers ask for the file name "
                                           "themselves and cannot be driven from here.".format(printer))
                results["exported"].append({"element_id": helpers.element_id_value(element.Id),
                                            "name": stem, "path": target})
                if note:
                    results["warnings"].append({"name": stem, "warning": note})
            except helpers.ExportCancelled:
                raise
            except Exception as error:
                log("ERROR: {0}".format(error))
                results["errors"].append({"name": label, "error": str(error)})
            finally:
                if restore_crop is not None:
                    restore_crop()

    try:
        if pdf24:
            if not os.path.isdir(temp):
                os.makedirs(temp)

            def waiting():
                report(0, "Another Revit is printing through PDF24 - waiting for it to finish (Cancel stops waiting)")
            with _Pdf24Turn(waiting) as turn:
                if turn.waited > 1.0:
                    results["warnings"].append({"name": "PDF24", "warning": "Another Revit was printing through PDF24; this export "
                                                "waited {0:.0f} s for it to finish.".format(turn.waited)})
                with _Pdf24AutoSave(temp):
                    run()
        else:
            run()
    except helpers.ExportCancelled as stop:
        stop.partial = results
        raise
    finally:
        for name, error in problems.items():
            results["warnings"].append({"name": "Print option",
                                        "warning": "{0} could not be set ({1})".format(name, error)})
        for folder in (temp, temp_root):                    # the run's own folder, then the shared one if nothing is left in it
            try:
                if os.path.isdir(folder) and not os.listdir(folder):
                    os.rmdir(folder)
            except OSError:
                pass
    return results
