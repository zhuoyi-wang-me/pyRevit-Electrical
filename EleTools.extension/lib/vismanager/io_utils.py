# -*- coding: utf-8 -*-
"""Export / import helpers for CSV and (optionally) XLSX.

XLSX support depends on `openpyxl` being available in whichever Python
environment pyRevit's engine is using (CPython3 engine only - IronPython
cannot import openpyxl). If it's missing we transparently fall back to
CSV and tell the calling script so it can inform the user.
"""
import csv
import io
import os

try:
    import openpyxl
    HAS_OPENPYXL = True
except Exception:
    HAS_OPENPYXL = False


def export_rows_csv(path, fieldnames, rows):
    with io.open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


def _cell_str(v):
    """Normalise a cell to text: None -> "", 5.0 -> "5" (Excel turns
    line weights / transparency into floats, which broke int() on import).
    """
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return (u"%s" % v).strip()


def _clean_rows(rows):
    """Strip header whitespace / stray BOMs and drop fully blank rows."""
    out = []
    for r in rows:
        clean = {}
        for k, v in r.items():
            if k is None:
                continue
            key = (u"%s" % k).replace(u"\ufeff", u"").strip()
            clean[key] = _cell_str(v)
        if any(clean.values()):
            out.append(clean)
    return out


def import_rows_csv(path):
    with io.open(path, "r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        return _clean_rows([dict(r) for r in reader])


def export_rows_xlsx(path, fieldnames, rows, sheet_title="VisManager"):
    if not HAS_OPENPYXL:
        # graceful fallback
        fallback = os.path.splitext(path)[0] + ".csv"
        return export_rows_csv(fallback, fieldnames, rows), False
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet_title[:31]
    ws.append(fieldnames)
    for row in rows:
        ws.append([row.get(k, "") for k in fieldnames])
    for col_idx, name in enumerate(fieldnames, start=1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(col_idx)].width = max(12, len(name) + 2)
    wb.save(path)
    return path, True


def import_rows_xlsx(path):
    if not HAS_OPENPYXL:
        raise RuntimeError(
            "openpyxl is not installed in pyRevit's Python engine. "
            "Export/import as CSV instead, or run "
            "'pip install openpyxl' in pyRevit's CPython environment."
        )
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []
    headers = [str(h) if h is not None else "" for h in rows[0]]
    out = []
    for r in rows[1:]:
        out.append(dict((headers[i], v) for i, v in enumerate(r) if i < len(headers)))
    return _clean_rows(out)


def export_rows(path, fieldnames, rows):
    """Dispatch on file extension. Returns (actual_path, used_xlsx)."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".xlsx":
        return export_rows_xlsx(path, fieldnames, rows)
    return export_rows_csv(path, fieldnames, rows), True


def import_rows(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".xlsx":
        return import_rows_xlsx(path)
    return import_rows_csv(path)
