# -*- coding: utf-8 -*-
"""pyRevit output report and CSV export for the SLD Checker."""
import io
import time

from sldcheck import checks, rules

_SEV_COLOR = {checks.ERROR: u"#c62828", checks.WARNING: u"#e08a00", checks.INFO: u"#607d8b"}


def _badge(severity):
    return u"<b style='color:{0}'>{1}</b>".format(_SEV_COLOR.get(severity, u"#000"), severity)


def _esc(text):
    return (rules.to_text(text).replace(u"&", u"&amp;").replace(u"<", u"&lt;")
            .replace(u">", u"&gt;"))


def _links(output, ids, id_map, title):
    eids = [id_map[i] for i in ids if i in id_map]
    # linked-model elements cannot be selected from the host - show their ids
    others = [rules.to_text(i) for i in ids if i not in id_map]
    text = u", ".join(others[:5]) + (u" ..." if len(others) > 5 else u"")
    if not eids:
        return _esc(text) if others else u"-"
    try:
        label = title if len(eids) > 1 else rules.to_text(ids[0])
        link = output.linkify(eids, title=label)
        return link + (u" + " + _esc(text) if others else u"")
    except Exception:
        return u", ".join(rules.to_text(i) for i in ids)


def counts_by_check(findings):
    table = dict((c, {checks.ERROR: 0, checks.WARNING: 0, checks.INFO: 0})
                 for c in checks.CHECK_ORDER)
    for item in findings:
        table[item["check"]][item["severity"]] += 1
    return table


def print_report(output, findings, ok_counts, stats, coverage, cfg, header_lines, id_map):
    output.set_width(1500)
    output.print_md(u"# SLD Checker")
    for line in header_lines:
        output.print_md(line)
    output.print_md(u"**Read-only:** nothing in the model was changed.")

    # ---- summary ----------------------------------------------------------
    output.print_md(u"## Summary")
    output.print_table(
        table_data=[
            [u"SLD board / equipment symbols", stats["sld_equipment"]],
            [u"SLD breaker / cable symbols", stats["sld_feeders"]],
            [u"SLD detail items ignored (no role / no data)", stats["sld_ignored"]],
            [u"Model electrical equipment", stats["model_equipment"]],
            [u"Model electrical circuits", stats["model_circuits"]],
            [u"Model electrical fixtures with a name", stats["model_fixtures"]],
            [u"Boards matched 1:1", stats["matched_equipment"]],
            [u"Feeders matched 1:1 to a circuit", stats["matched_feeders"]],
            [u"Outgoing feeders (circuit feeds a board)", stats["outgoing_feeders"]],
        ],
        columns=[u"Item", u"Count"])
    table = counts_by_check(findings)
    rows = []
    for check in checks.CHECK_ORDER:
        c = table[check]
        rows.append([check, c[checks.ERROR], c[checks.WARNING], c[checks.INFO], ok_counts[check]])
    output.print_table(table_data=rows,
                       columns=[u"Check", u"Errors", u"Warnings", u"Info", u"Passed"])

    # ---- mapping coverage ---------------------------------------------------
    output.print_md(u"## SLD parameter mapping found")
    cov_rows = []
    for field in sorted(coverage):
        count, names = coverage[field]
        cov_rows.append([field, count, u", ".join(names) if names else
                         u"<span style='color:#999'>not found - tried: {0}</span>".format(
                             _esc(u", ".join(cfg["sld_fields"][field])))])
    output.print_table(table_data=cov_rows,
                       columns=[u"Field", u"Detail items with value", u"Parameter used"])
    output.print_md(u"Fields showing **0** are not compared - map them on the "
                    u"*SLD Parameters* tab of the setup window and save the template.")

    # ---- findings per check -------------------------------------------------
    limit = int(cfg.get("max_rows_per_section", 400) or 400)
    for check in checks.CHECK_ORDER:
        items = [f for f in findings if f["check"] == check]
        output.print_md(u"## {0}".format(check))
        if not items:
            output.print_md(u"No issues ({0} comparison(s) passed).".format(ok_counts[check]))
            continue
        rows = []
        for item in items[:limit]:
            rows.append([
                _badge(item["severity"]), _esc(item["issue"]), _esc(item["key"]),
                _esc(item["message"]), _esc(item["sld"]), _esc(item["model"]),
                _links(output, item["sld_ids"], id_map, u"{0} SLD items".format(len(item["sld_ids"]))),
                _links(output, item["model_ids"], id_map,
                       u"{0} model elements".format(len(item["model_ids"]))),
                _esc(item["view"])])
        output.print_table(table_data=rows, columns=[
            u"Severity", u"Issue", u"Panel / Feeder", u"Details", u"SLD value",
            u"Model value", u"SLD item", u"Model element", u"View"])
        if len(items) > limit:
            output.print_md(u"*... {0} more row(s) - see the CSV export.*".format(len(items) - limit))


def _csv_cell(value):
    text = rules.to_text(value)
    if any(ch in text for ch in u',"\r\n'):
        text = u'"' + text.replace(u'"', u'""') + u'"'
    return text


CSV_COLUMNS = [u"Check", u"Severity", u"Issue", u"Panel / Feeder", u"Details", u"SLD value",
               u"Model value", u"SLD element ids", u"Model element ids", u"View"]


def csv_rows(findings):
    for item in findings:
        yield [item["check"], item["severity"], item["issue"], item["key"], item["message"],
               item["sld"], item["model"],
               u";".join(rules.to_text(i) for i in item["sld_ids"]),
               u";".join(rules.to_text(i) for i in item["model_ids"]), item["view"]]


def _plain(text):
    """Header lines are markdown - drop the ** / * markers for the text log."""
    return rules.to_text(text).replace(u"**", u"").strip(u"*")


def issue_breakdown(findings):
    """{(check, severity, issue): count} - the quickest way to see what dominates."""
    table = {}
    for item in findings:
        key = (item["check"], item["severity"], item["issue"])
        table[key] = table.get(key, 0) + 1
    return table


def write_log(path, header_lines, stats, coverage, cfg, findings, ok_counts, error_text=None):
    """Plain-text run log (last_run.txt) - everything needed to diagnose a run
    without the pyRevit output window: header, counts, mapping used, all rows."""
    lines = [u"SLD Checker run " + time.strftime("%Y-%m-%d %H:%M:%S")]
    lines.extend(u"   " + _plain(h) for h in header_lines or [])
    if error_text:
        lines.append(u"FAILED:")
        lines.extend(u"   " + l for l in rules.to_text(error_text).splitlines())
    if stats:
        lines.append(u"counts:")
        for key in sorted(stats):
            value = stats[key]
            if isinstance(value, dict):        # per-panel table: keep the log readable
                busy = sorted(value.items(), key=lambda kv: -kv[1])[:10]
                value = u"{0} panel(s); most feeders: {1}".format(
                    len(value), u", ".join(u"{0}={1}".format(k, v) for k, v in busy if v) or u"none")
            lines.append(u"   {0}: {1}".format(key, value))
    if cfg:
        lines.append(u"SLD field -> parameter names tried:")
        for field in sorted(cfg.get("sld_fields", {})):
            lines.append(u"   {0}: {1}".format(field, u", ".join(cfg["sld_fields"][field])))
        for section in ("model_equipment_fields", "model_circuit_fields", "model_fixture_fields"):
            lines.append(u"{0}:".format(section))
            for field in sorted(cfg.get(section, {})):
                lines.append(u"   {0}: {1}".format(field, u", ".join(cfg[section][field])))
    if coverage:
        lines.append(u"SLD mapping found (detail items with a value / parameter used):")
        for field in sorted(coverage):
            count, names = coverage[field]
            lines.append(u"   {0}: {1} via {2}".format(field, count, u", ".join(names) or u"NOT FOUND"))
    if findings is not None:
        lines.append(u"per check (errors / warnings / info / passed):")
        table = counts_by_check(findings)
        for check in checks.CHECK_ORDER:
            c = table[check]
            lines.append(u"   {0}: {1} / {2} / {3} / {4}".format(
                check, c[checks.ERROR], c[checks.WARNING], c[checks.INFO],
                (ok_counts or {}).get(check, 0)))
        lines.append(u"per issue type:")
        breakdown = issue_breakdown(findings)
        for key in sorted(breakdown, key=lambda k: (-breakdown[k], k)):
            lines.append(u"   {0:5d}  {1} | {2} | {3}".format(breakdown[key], *key))
        lines.append(u"rows ({0}):".format(len(findings)))
        lines.append(u"\t".join(CSV_COLUMNS))
        for row in csv_rows(findings):
            lines.append(u"\t".join(rules.to_text(c).replace(u"\t", u" ").replace(u"\n", u" ")
                                    for c in row))
    with io.open(path, "w", encoding="utf-8") as handle:
        handle.write(u"\n".join(lines) + u"\n")
    return path


def write_csv(path, findings):
    """UTF-8 with BOM so Excel shows mm² and arrows correctly."""
    with io.open(path, "w", encoding="utf-8-sig", newline=u"") as handle:
        handle.write(u",".join(CSV_COLUMNS) + u"\r\n")
        for row in csv_rows(findings):
            handle.write(u",".join(_csv_cell(c) for c in row) + u"\r\n")
    return path
