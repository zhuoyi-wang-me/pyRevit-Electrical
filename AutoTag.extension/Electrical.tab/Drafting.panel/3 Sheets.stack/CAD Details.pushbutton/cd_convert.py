# -*- coding: utf-8 -*-
"""CAD Details - DWG -> DXF with AutoCAD's own console engine (accoreconsole.exe), no AutoCAD window.

Revit's API gives only the lines and arcs of an imported DWG - no text, no hatches. AutoCAD's console
opens the DWG, turns visible block attributes into plain text, explodes blocks and dimensions (so their
lines, arrowheads and text are plain shapes), and saves a DXF that cd_dxf.py reads completely.
The DWG itself is never saved - it is opened, converted and closed.
"""
import glob
import io
import os
import re
import time

TEMP = os.path.join(os.environ.get('TEMP', os.path.expanduser('~')), 'EleTools', 'CadDetails')
TIMEOUT_S = 180

SCRIPT = u'''(setvar "CMDECHO" 0)
(setvar "FILEDIA" 0)
(setq ss (ssget "_X" '((0 . "INSERT") (66 . 1))))
(if ss (progn (setq i (sslength ss))
  (while (> i 0) (setq i (1- i) e (ssname ss i) a (entnext e))
    (while (and a (= (cdr (assoc 0 (setq ad (entget a)))) "ATTRIB"))
      (if (= 0 (logand 1 (cdr (assoc 70 ad))))
        (entmake (list '(0 . "TEXT") (assoc 8 ad) (assoc 10 ad) (assoc 40 ad) (assoc 1 ad)
                       (cons 50 (cdr (assoc 50 ad))) (cons 72 (cdr (assoc 72 ad))) (assoc 11 ad)
                       (cons 73 (cdr (assoc 74 ad))))))
      (setq a (entnext a))))))
(setvar "QAFLAGS" 1)
(setq n 0)
(while (and (< n 12) (setq ss (ssget "_X" '((0 . "INSERT,DIMENSION,LEADER,MLEADER,MULTILEADER,TABLE,REGION")))))
  (command "_.EXPLODE" ss "")
  (setq n (1+ n)))
(setvar "QAFLAGS" 0)
(if (setq ss (ssget "_X" '((0 . "ATTDEF")))) (command "_.ERASE" ss ""))
(command "_.DXFOUT" "{dxf}" "_V" "2018" "16")
(command "_.QUIT" "_Y")
'''


def find_console():
    """accoreconsole.exe of the newest AutoCAD (any flavour: AutoCAD, AutoCAD MEP, Civil 3D ...), or None."""
    found = []
    for root in (os.environ.get('ProgramFiles', r'C:\Program Files'), os.environ.get('ProgramW6432', r'C:\Program Files')):
        for path in glob.glob(os.path.join(root, 'Autodesk', 'AutoCAD*', 'accoreconsole.exe')):
            match = re.search(r'(\d{4})', os.path.basename(os.path.dirname(path)))
            found.append((int(match.group(1)) if match else 0, path))
    if not found:
        return None
    return sorted(set(found))[-1][1]


def to_dxf(path, log=None):
    """The DXF to read for `path`: a .dxf as it is; a .dwg converted by AutoCAD's console.
    Raises ValueError with a message for the user when it cannot be done."""
    if path.lower().endswith('.dxf'):
        return path
    if not path.lower().endswith('.dwg'):
        raise ValueError(u'Only DWG (or DXF) files can be read.')
    console = find_console()
    if console is None:
        raise ValueError(u'AutoCAD (accoreconsole.exe) was not found on this PC: it is needed to read the text and '
                         u'hatches of a DWG. Save the DWG as DXF in AutoCAD and pick the DXF instead.')
    if not os.path.isdir(TEMP):
        os.makedirs(TEMP)
    stem = re.sub(r'[^A-Za-z0-9_\-]', '_', os.path.splitext(os.path.basename(path))[0])[:60]
    stamp = time.strftime('%Y%m%d_%H%M%S')
    dxf = os.path.join(TEMP, u'{0}_{1}.dxf'.format(stem, stamp))
    script = os.path.join(TEMP, u'{0}_{1}.scr'.format(stem, stamp))
    out = os.path.join(TEMP, u'{0}_{1}.log'.format(stem, stamp))
    with io.open(script, 'w', encoding='cp1252', errors='replace') as handle:
        handle.write(SCRIPT.replace(u'{dxf}', dxf.replace(u'\\', u'/')))
    import System.Diagnostics as diagnostics
    info = diagnostics.ProcessStartInfo(console, u'/i "{0}" /s "{1}" /l en-US'.format(path, script))
    info.UseShellExecute = False
    info.CreateNoWindow = True
    info.RedirectStandardOutput = True
    info.RedirectStandardInput = True
    process = diagnostics.Process.Start(info)
    process.StandardInput.Close()                         # no questions answered: a prompt ends the run
    text = process.StandardOutput.ReadToEndAsync()
    if not process.WaitForExit(TIMEOUT_S * 1000):
        try:
            process.Kill()
        except Exception:
            pass
        raise ValueError(u'AutoCAD did not finish converting the DWG within {0} s.'.format(TIMEOUT_S))
    try:
        with io.open(out, 'w', encoding='utf-8') as handle:
            handle.write(u'{0}'.format(text.Result or u'').replace(u'\x00', u''))
    except Exception:
        pass
    if log is not None:
        log(u'accoreconsole: {0}, exit {1}, log {2}'.format(console, process.ExitCode, out))
    if not os.path.isfile(dxf) or os.path.getsize(dxf) < 100:
        raise ValueError(u'AutoCAD could not convert the DWG (see {0}). Is the file open in AutoCAD with unsaved '
                         u'changes, or from a newer AutoCAD version?'.format(out))
    return dxf
