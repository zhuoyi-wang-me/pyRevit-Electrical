# -*- coding: utf-8 -*-
"""SLD Checker - review single line diagrams drawn with Detail Items against
the electrical model and report every discrepancy. READ-ONLY.

Workflow:
1. The setup window opens with the last used TEMPLATE:
   - Scope & Checks: which SLD views, which checks to report
   - SLD / Model Parameters: map each field to a parameter; the drop-downs
     list the parameters really found in this model, with a live
     "value on N elements, e.g. ..." preview and Auto-detect
   - Recognition & Options: symbol keywords, MDB/SMDB/DB rules, tolerances
   Save / Save As / Import / Export templates to reuse them on any project.
2. Run Check: matching by Panel / Equipment name (trimmed, case-insensitive);
   duplicates and ambiguous names are reported and never compared.
3. Report grouped by check with clickable element links, CSV export.

pyRevit (IronPython 2.7), Revit 2025 / 2026.
"""
import time

from pyrevit import forms, revit, script

from sldcheck import checks, collect, config, report
from sld_window import SetupWindow

doc = revit.doc
output = script.get_output()
logger = script.get_logger()


def export_csv(findings):
    if not findings:
        return
    if not forms.alert(u"Export all {0} result rows to CSV?".format(len(findings)),
                       yes=True, no=True):
        return
    default = u"SLD_Check_{0}_{1}".format(
        (doc.Title or u"model").replace(u" ", u"_"), time.strftime("%Y%m%d_%H%M"))
    path = forms.save_file(file_ext="csv", default_name=default)
    if not path:
        return
    try:
        report.write_csv(path, findings)
        output.print_md(u"**CSV exported:** `{0}`".format(path))
    except Exception as error:
        forms.alert(u"Could not write the CSV:\n{0}".format(error))


def main():
    if doc is None or doc.IsFamilyDocument:
        forms.alert(u"Open a project model (not a family) to check its SLD.", exitscript=True)

    config.migrate_legacy()
    views = collect.views_with_detail_items(doc)
    if not views:
        forms.alert(u"No Detail Items found in any view - nothing to check.", exitscript=True)
    view_rows = sorted([(key, collect.safe_name(view, u"<view>"), rules_text(view.ViewType), count)
                        for key, (view, count) in views.items()], key=lambda r: r[1].lower())

    with forms.ProgressBar(title=u"SLD Checker - reading parameter names...", indeterminate=True):
        discovery = collect.discover_parameters(doc)

    window = SetupWindow(view_rows, collect.id_value(doc.ActiveView.Id), discovery,
                         config.get_last_template())
    window.ShowDialog()
    choice = window.result
    if not choice:
        script.exit()
    cfg = choice["cfg"]

    started = time.time()
    id_map = {}
    reader = collect.ParamReader(doc, cfg["units"])
    links = []
    with forms.ProgressBar(title=u"SLD Checker - checking...", indeterminate=True):
        details = collect.collect_details(doc, cfg, choice["view_ids"], reader, id_map)
        models = collect.collect_model(doc, cfg, reader, id_map, links)
        result, stats = checks.run_checks(details, models, cfg, choice["mode"] == "all")
    findings = result.sorted()
    coverage = collect.mapping_coverage(details, cfg)

    enabled = checks.enabled_checks(cfg)
    off = [label for key, _g, label, _l in checks.CHECK_ITEMS if not enabled[key]]
    scope = {"all": u"all {0} views with Detail Items",
             "active": u"active view '{1}'",
             "pick": u"{0} picked view(s)"}[choice["mode"]].format(
                 choice["view_count"], collect.safe_name(doc.ActiveView))
    header = [
        u"**Model:** {0}".format(doc.Title),
        u"**Template:** {0}".format(choice["template"] or config.DEFAULT_TEMPLATE),
        u"**Scope:** {0}".format(scope),
        u"**Time:** {0:.1f} s - {1} result row(s){2}".format(
            time.time() - started, len(findings),
            u", {0} hidden by switched-off checks / INFO".format(result.hidden)
            if result.hidden else u""),
    ]
    if off:
        header.append(u"**Checks switched off:** {0}".format(u"; ".join(off)))
    if choice["mode"] != "all":
        header.append(u"*Partial scope: 'Missing Detail Item' only lists model boards / "
                      u"feeders whose source board is drawn in the checked views.*")
    if not cfg.get("include_linked_models"):
        header.append(u"*Host model only - equipment in linked models is not checked.*")
    else:
        used = [(n, c) for n, c in links if c]
        empty = len([1 for n, c in links if c == 0])
        unloaded = [n for n, c in links if c is None]
        if used:
            header.append(u"**Linked models matched:** {0}{1}".format(u"; ".join(
                u"{0} ({1} elements)".format(name, count) for name, count in used),
                u" (+{0} loaded link(s) without electrical equipment)".format(empty) if empty else u""))
        elif not unloaded:
            header.append(u"*No linked model with electrical equipment - host model only.*")
        if unloaded:
            header.append(u"**Warning:** {0} Revit link(s) are NOT loaded - boards in them show as "
                          u"'Missing model element': {1}".format(len(unloaded), u"; ".join(unloaded)))
    if stats["model_equipment"] and stats["model_circuits"] < 0.1 * stats["model_equipment"]:
        header.append(
            u"**Warning:** the model has only {0} power circuit(s) for {1} boards - the "
            u"equipment is not circuited, so circuit / breaker / cable checks find nothing "
            u"to compare. Fed From falls back to the board's own MEE-FedFrom parameter."
            .format(stats["model_circuits"], stats["model_equipment"]))
    log = None
    try:
        log = report.write_log(config.log_path(), header, stats, coverage, cfg, findings, result.ok)
    except Exception as error:
        logger.warning(u"Could not write the run log: {0}".format(error))

    # QA/QC preview (modeless, like Cable Tray QA/QC). If it cannot open, fall
    # back to the classic report in the pyRevit output window.
    try:
        import results_window
        results_window.show(doc, findings, stats, coverage, cfg, header, id_map, log)
    except Exception as error:
        logger.warning(u"Results window failed ({0}) - showing the output report.".format(error))
        report.print_report(output, findings, result.ok, stats, coverage, cfg, header, id_map)
        if log:
            output.print_md(u"**Run log:** `{0}`".format(log))
        export_csv(findings)


def rules_text(value):
    try:
        return u"{0}".format(value)
    except Exception:
        return u"?"


try:
    main()
except SystemExit:
    raise
except Exception as error:
    logger.error(u"SLD Checker failed: {0}".format(error))
    import traceback
    trace = traceback.format_exc()
    output.print_md(u"```\n{0}\n```".format(trace))
    try:
        report.write_log(config.log_path(), [u"Model: {0}".format(doc.Title)], None, None,
                         None, None, None, error_text=trace)
    except Exception:
        pass
