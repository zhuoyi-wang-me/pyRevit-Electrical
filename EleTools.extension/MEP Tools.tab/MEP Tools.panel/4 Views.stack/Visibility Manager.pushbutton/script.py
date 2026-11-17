# -*- coding: utf-8 -*-
"""Visibility Manager v1.5 - Filter, V/G (host model) and Revit Link
visibility management across views and view templates.

Runs on pyRevit's IronPython engine (CPython engine also works).

v1.1 fixes (see README "Changelog"):
- Revit 2026: no more ElementId.IntegerValue (removed from the API).
- View-template awareness: source settings are read from the template
  that actually controls them; target views controlled by a template are
  skipped with a clear reason instead of "succeeding" with no effect.
- Links: "By Linked View" now copies its linked view; view-type
  mismatches are remapped or reported.
- V/G: subcategories included; visibility and graphics applied
  independently; import restores Visible as well as Hidden.
- Filters: import restores Enabled / Visible flags.
- Each item runs in its own SubTransaction; warnings are suppressed so
  one warning can't roll back the whole batch; a rolled-back
  transaction is reported instead of claiming success.
- Target lists start UNCHECKED (previously every view in the project
  was pre-selected), keep their checks when type chips change, and only
  items visible under the current search are acted on.
"""
import os

import clr
clr.AddReference("PresentationCore")
clr.AddReference("PresentationFramework")
clr.AddReference("WindowsBase")
clr.AddReference("System.Xaml")

from System.Windows import Visibility, Thickness, FontStyles
from System.Windows.Media import SolidColorBrush, Color as MediaColor
from System.Windows.Controls import CheckBox, TextBlock, StackPanel, Orientation

from pyrevit import revit, DB, forms, script

from vismanager import view_utils, filters_core, vg_core, links_core, worksets_core, io_utils, compat
from vismanager.compat import eid_val
import safetable  # crash-safe output tables (lib/safetable.py)

logger = script.get_logger()
output = script.get_output()

doc = revit.doc
uidoc = revit.uidoc

GROUP_TO_VIEW_CHIP = {
    "3D Views": "chkV3D", "Floor Plans": "chkVFloor", "Sections": "chkVSection",
    "Ceiling Plans": "chkVCeiling", "Elevations": "chkVElevation",
}
GROUP_TO_TEMPLATE_CHIP = {
    "3D Views": "chkT3D", "Floor Plans": "chkTFloor", "Sections": "chkTSection",
    "Ceiling Plans": "chkTCeiling", "Elevations": "chkTElevation",
}

NOUN = {"filters": "filter", "vg": "category", "links": "link", "worksets": "workset"}
NOUN_PL = {"filters": "filters", "vg": "categories", "links": "links", "worksets": "worksets"}


def brush(hex_str):
    c = MediaColor.FromRgb(int(hex_str[0:2], 16), int(hex_str[2:4], 16), int(hex_str[4:6], 16))
    return SolidColorBrush(c)


GREEN = "2E7D32"
BLUE = "1565C0"
PURPLE = "6A1B9A"
ORANGE = "E65100"


def make_checkbox(name, status="", checked=False, tag=None):
    """CheckBox with 'Name  (status)' - status small, gray, italic."""
    cb = CheckBox()
    cb.Margin = Thickness(2, 3, 2, 3)
    cb.IsChecked = checked
    cb.Tag = tag
    if not status:
        cb.Content = name
        return cb
    panel = StackPanel()
    panel.Orientation = Orientation.Horizontal
    t1 = TextBlock()
    t1.Text = name
    panel.Children.Add(t1)
    t2 = TextBlock()
    t2.Text = u"  ({})".format(status)
    t2.Foreground = brush("888888")
    t2.FontStyle = FontStyles.Italic
    t2.FontSize = 10.5
    panel.Children.Add(t2)
    cb.Content = panel
    return cb


class VisManagerWindow(forms.WPFWindow):
    def __init__(self, xaml_file):
        # FIX: state must exist BEFORE the XAML loads - RadioButton/CheckBox
        # events can fire while the window is being constructed.
        self._ready = False
        self.mode = "filters"
        self.active_view = doc.ActiveView
        self.source_controls = {}
        self.view_controls = {}
        self.template_controls = {}
        self.view_checked = {}       # id -> bool, survives chip/search refreshes
        self.template_checked = {}
        self.source_data = []
        self.view_data = []
        self.template_data = []

        av = self.active_view
        if av is None or not isinstance(av, DB.View) or av.IsTemplate:
            forms.alert("No active graphical view. Open a view and try again.", exitscript=True)
        try:
            if not av.AreGraphicsOverridesAllowed():
                forms.alert(u"The active view '{}' doesn't support Visibility/Graphics "
                            u"overrides. Open a plan, section, elevation or 3D view."
                            .format(av.Name), exitscript=True)
        except AttributeError:
            pass

        forms.WPFWindow.__init__(self, xaml_file)

        self.all_views, self.all_templates = view_utils.get_all_views_and_templates(
            doc, exclude_view_id=av.Id)
        self._apply_type_counts()

        self._ready = True
        self.refresh_source_list()
        self.refresh_view_list()
        self.refresh_template_list()

    # ------------------------------------------------------------- setup
    def _apply_type_counts(self):
        vc = view_utils.group_counts(self.all_views)
        tc = view_utils.group_counts(self.all_templates)
        for group, chip in GROUP_TO_VIEW_CHIP.items():
            getattr(self, chip).Content = "{} ({})".format(group, vc.get(group, 0))
        for group, chip in GROUP_TO_TEMPLATE_CHIP.items():
            getattr(self, chip).Content = "{} ({})".format(group, tc.get(group, 0))

    # ---------------------------------------------------------------- mode
    def _set_mode(self, mode, bg, fg, title, body, export_cap, import_cap):
        self.mode = mode
        self.borderModeBanner.Background = brush(bg)
        self.txtModeBannerTitle.Foreground = brush(fg)
        self.txtModeBannerTitle.Text = title
        self.txtModeBannerBody.Foreground = brush(fg)
        self.txtModeBannerBody.Text = body
        self.btnCopy.Background = brush(fg)
        self.txtExportCaption.Text = export_cap
        self.txtImportCaption.Text = import_cap
        if self._ready:
            self.refresh_source_list()

    def OnModeFiltersClick(self, sender, args):
        if not getattr(self, "_ready", False):
            return
        self._set_mode("filters", "DDF3DD", GREEN, "Filters Mode",
                       "Select filters from current view to copy/remove to selected targets [2] & [3]",
                       "Export current view Filters", "Import Filters to checked targets")

    def OnModeVGClick(self, sender, args):
        if not getattr(self, "_ready", False):
            return
        self._set_mode("vg", "DCEEFB", BLUE, "V/G Overrides Mode (host model)",
                       "Select host-model categories / subcategories from current view to "
                       "copy/reset on selected targets [2] & [3]",
                       "Export current view V/G Overrides", "Import V/G Overrides to checked targets")

    def OnModeLinksClick(self, sender, args):
        if not getattr(self, "_ready", False):
            return
        if not links_core.LINKS_API_AVAILABLE:
            forms.alert("Revit Links display-settings management isn't available in this "
                        "Revit version (View.SetLinkOverrides not found). Staying in Filters mode.",
                        title="Not available")
            self.radModeFilters.IsChecked = True
            return
        self._set_mode("links", "F0E6F6", PURPLE, "Revit Links Mode",
                       "Select linked models' display settings from current view to copy/reset "
                       "on selected targets [2] & [3]",
                       "Export current view Revit Links", "Import Revit Links to checked targets")

    def OnModeWorksetsClick(self, sender, args):
        if not getattr(self, "_ready", False):
            return
        if not worksets_core.available(doc):
            forms.alert("This model isn't workshared, so it has no worksets. "
                        "Staying in Filters mode.", title="Not available")
            self.radModeFilters.IsChecked = True
            return
        self._set_mode("worksets", "FFF0E0", ORANGE, "Worksets Mode (host model)",
                       "Select worksets from current view to copy their visibility "
                       "(Shown / Hidden / Using Global Setting) or reset them on targets [2] & [3]",
                       "Export current view Worksets", "Import Worksets to checked targets")

    # ------------------------------------------------------------ source
    def _source_key(self, item):
        """Which view-template Include row governs this item."""
        if self.mode == "filters":
            return "filters"
        if self.mode == "links":
            return "links"
        if self.mode == "worksets":
            return "worksets"
        return item.get("template_key", "Model")

    def refresh_source_list(self):
        self.stkSourceItems.Children.Clear()
        self.source_controls = {}
        self.source_data = []
        av = self.active_view
        template_sources = set()

        if self.mode == "filters":
            src_view = compat.effective_view(av, "filters")
            if src_view is not av:
                template_sources.add(src_view.Name)
            for fid, name in filters_core.get_view_filters(src_view):
                try:
                    vis = src_view.GetFilterVisibility(fid)
                    en = src_view.GetIsFilterEnabled(fid)
                    status = ", ".join([t for t, on in (("Disabled", not en), ("Hidden", not vis)) if on])
                except Exception:
                    status = ""
                self.source_data.append({"id": eid_val(fid), "name": name, "obj": fid,
                                         "status": status, "src_view": src_view})
        elif self.mode == "vg":
            for c in vg_core.get_categories(doc, av):
                src_view = compat.effective_view(av, c["template_key"])
                if src_view is not av:
                    template_sources.add(src_view.Name)
                self.source_data.append({
                    "id": eid_val(c["cat"].Id), "name": c["name"], "obj": c["cat"],
                    "status": vg_core.category_status_label(src_view, c["cat"]),
                    "src_view": src_view, "template_key": c["template_key"]})
        elif self.mode == "links":
            src_view = compat.effective_view(av, "links")
            if src_view is not av:
                template_sources.add(src_view.Name)
            for link_id, name in links_core.get_project_links(doc):
                tags = [t for t in (links_core.element_status(av, link_id),
                                    links_core.link_status_label(src_view, link_id)) if t]
                self.source_data.append({
                    "id": eid_val(link_id), "name": name, "obj": link_id,
                    "status": ", ".join(tags),
                    # display settings come from the controlling template (if any);
                    # Visibility/Halftone are element state and always live on the view
                    "src_view": src_view, "elem_src": av})
        else:  # worksets
            src_view = compat.effective_view(av, "worksets")
            if src_view is not av:
                template_sources.add(src_view.Name)
            for wid, name in worksets_core.get_worksets(doc):
                self.source_data.append({
                    "id": eid_val(wid), "name": name, "obj": wid,
                    "status": worksets_core.status_label(src_view, wid),
                    "src_view": src_view})
        self.source_data.sort(key=lambda d: d["name"].lower())

        for item in self.source_data:
            cb = make_checkbox(item["name"], item["status"], checked=True, tag=item["id"])
            cb.Checked += self._on_source_check
            cb.Unchecked += self._on_source_check
            self.stkSourceItems.Children.Add(cb)
            self.source_controls[item["id"]] = cb

        self.chkSourceSelectAll.IsChecked = True
        self.OnSourceSearchChanged(None, None)
        self._update_source_count()
        if compat.in_temporary_view_properties(av):
            self.txtStatus.Text = ("Active view is in Temporary View Properties mode - what you "
                                   "see on screen is NOT what will be copied. Turn it off first.")
        elif template_sources:
            self.txtStatus.Text = (u"Active view is controlled by template '{}' - showing "
                                   u"and copying the template's settings."
                                   .format("', '".join(sorted(template_sources))))
        else:
            self.txtStatus.Text = "Ready."

    def _on_source_check(self, sender, args):
        self._update_source_count()

    def _update_source_count(self):
        n = len(self.get_selected_source())
        self.txtSourceCount.Text = "{} {} selected".format(n, NOUN_PL[self.mode])

    def OnSourceSearchChanged(self, sender, args):
        self._apply_search(self.txtSourceSearch, self.source_data, self.source_controls)
        if getattr(self, "_ready", False):
            self._update_source_count()

    def OnSourceSelectAllClick(self, sender, args):
        self._select_visible(self.source_controls, self.chkSourceSelectAll)
        self._update_source_count()

    def get_selected_source(self):
        return [d for d in self.source_data if self._is_active(self.source_controls.get(d["id"]))]

    # --------------------------------------------------------- targets
    def refresh_view_list(self):
        allowed = self._allowed_groups(GROUP_TO_VIEW_CHIP)
        self.view_data = sorted([e for e in self.all_views if e["group"] in allowed],
                                key=lambda d: d["name"].lower())
        self._populate_targets(self.stkViewItems, self.view_data, self.view_controls,
                               self.view_checked, self._on_view_check, show_template=True)
        self._apply_search(self.txtViewSearch, self.view_data, self.view_controls)
        self._update_view_count()

    def refresh_template_list(self):
        allowed = self._allowed_groups(GROUP_TO_TEMPLATE_CHIP)
        self.template_data = sorted([e for e in self.all_templates if e["group"] in allowed],
                                    key=lambda d: d["name"].lower())
        self._populate_targets(self.stkTemplateItems, self.template_data, self.template_controls,
                               self.template_checked, self._on_template_check, show_template=False)
        self._apply_search(self.txtTemplateSearch, self.template_data, self.template_controls)
        self._update_template_count()

    def OnViewTypeFilterChanged(self, sender, args):
        self.refresh_view_list()

    def OnTemplateTypeFilterChanged(self, sender, args):
        self.refresh_template_list()

    def _on_view_check(self, sender, args):
        self.view_checked[sender.Tag] = bool(sender.IsChecked)
        self._update_view_count()

    def _on_template_check(self, sender, args):
        self.template_checked[sender.Tag] = bool(sender.IsChecked)
        self._update_template_count()

    def _update_view_count(self):
        n = sum(1 for cb in self.view_controls.values() if self._is_active(cb))
        self.txtViewCount.Text = "{} views selected".format(n)
        self.chkViewsSelectAll.IsChecked = self._all_visible_checked(self.view_controls)

    def _update_template_count(self):
        n = sum(1 for cb in self.template_controls.values() if self._is_active(cb))
        self.txtTemplateCount.Text = "{} templates selected".format(n)
        self.chkTemplatesSelectAll.IsChecked = self._all_visible_checked(self.template_controls)

    def OnViewSearchChanged(self, sender, args):
        self._apply_search(self.txtViewSearch, self.view_data, self.view_controls)
        if getattr(self, "_ready", False):
            self._update_view_count()

    def OnTemplateSearchChanged(self, sender, args):
        self._apply_search(self.txtTemplateSearch, self.template_data, self.template_controls)
        if getattr(self, "_ready", False):
            self._update_template_count()

    def OnViewsSelectAllClick(self, sender, args):
        self._select_visible(self.view_controls, self.chkViewsSelectAll)
        self._update_view_count()

    def OnTemplatesSelectAllClick(self, sender, args):
        self._select_visible(self.template_controls, self.chkTemplatesSelectAll)
        self._update_template_count()

    def get_selected_targets(self):
        t = [d for d in self.view_data if self._is_active(self.view_controls.get(d["id"]))]
        t += [d for d in self.template_data if self._is_active(self.template_controls.get(d["id"]))]
        return t

    # --------------------------------------------------------- shared UI
    @staticmethod
    def _is_active(cb):
        """Checked AND visible under the current search (FIX: hidden-by-
        search items used to be acted on silently)."""
        return cb is not None and bool(cb.IsChecked) and cb.Visibility == Visibility.Visible

    @staticmethod
    def _all_visible_checked(controls):
        vis = [cb for cb in controls.values() if cb.Visibility == Visibility.Visible]
        return bool(vis) and all(bool(cb.IsChecked) for cb in vis)

    @staticmethod
    def _select_visible(controls, master):
        state = bool(master.IsChecked)
        for cb in controls.values():
            if cb.Visibility == Visibility.Visible:
                cb.IsChecked = state

    @staticmethod
    def _apply_search(textbox, data, controls):
        q = (textbox.Text or "").strip().lower()
        for item in data:
            cb = controls.get(item["id"])
            if cb is not None:
                cb.Visibility = Visibility.Visible if q in item["name"].lower() else Visibility.Collapsed

    def _allowed_groups(self, groups):
        return set(g for g, chip in groups.items() if getattr(self, chip).IsChecked)

    def _populate_targets(self, panel, data, controls, checked_state, handler, show_template):
        panel.Children.Clear()
        controls.clear()
        for item in data:
            status = u"template: {}".format(item["template_name"]) \
                if show_template and item.get("template_name") else ""
            cb = make_checkbox(item["name"], status,
                               checked=checked_state.get(item["id"], False), tag=item["id"])
            cb.Checked += handler
            cb.Unchecked += handler
            panel.Children.Add(cb)
            controls[item["id"]] = cb

    # ------------------------------------------------------ batch engine
    def _run_batch(self, title, targets, jobs, prepare=None, cleanup=None):
        """jobs: list of dicts {name, key, apply(target_view) -> "changed"|"same"}.
        apply() must RAISE if the result doesn't match what was intended
        (read-back verification). Each (target, job) runs in its own
        SubTransaction. Targets whose template controls `key` are skipped.
        Returns a result dict.
        """
        res = {"changed": 0, "same": 0, "fail": 0, "skipped": 0, "msgs": [], "per_target": []}
        skip_notes, include_notes, na_notes = {}, set(), set()

        with DB.Transaction(doc, "Visibility Manager - " + title) as t:
            t.Start()
            compat.attach_warning_swallower(t)
            if prepare is not None:
                try:
                    prepare()
                except Exception as ex:
                    t.RollBack()
                    forms.alert(u"Couldn't prepare the copy: {}".format(ex), title="Visibility Manager")
                    res["fail"] = len(targets) * len(jobs)
                    res["msgs"].append(u"FAILED setup: {}".format(ex))
                    return res
            for tgt in targets:
                tv = tgt["obj"]
                row = {"name": tgt["name"], "changed": 0, "same": 0, "fail": 0, "skipped": 0}
                if compat.in_temporary_view_properties(tv):
                    res["msgs"].append(u"NOTE '{}' is in Temporary View Properties mode - Revit is "
                                       u"displaying a temporary template, so changes won't show until "
                                       u"you turn it off (view control bar).".format(tgt["name"]))
                for job in jobs:
                    ctrl = compat.controlling_template(tv, job["key"])
                    if ctrl is not None:
                        row["skipped"] += 1
                        k = (tgt["name"], ctrl.Name, job["key"])
                        skip_notes[k] = skip_notes.get(k, 0) + 1
                        continue
                    if tv.IsTemplate and compat.template_include_off(tv, job["key"]):
                        include_notes.add((tgt["name"], job["key"]))
                    st = DB.SubTransaction(doc)
                    st.Start()
                    try:
                        outcome = job["apply"](tv)
                        st.Commit()
                        if outcome == "same":
                            row["same"] += 1
                        elif outcome == "n/a":
                            row["skipped"] += 1
                            if job.get("na_note"):
                                na_notes.add((tgt["name"], job["na_note"]))
                        else:
                            row["changed"] += 1
                            if isinstance(outcome, tuple) and outcome[1]:
                                res["msgs"].append(u"NOTE {} -> {}: {}".format(
                                    job["name"], tgt["name"], outcome[1]))
                    except Exception as ex:
                        try:
                            if st.HasStarted() and not st.HasEnded():
                                st.RollBack()
                        except Exception:
                            pass
                        row["fail"] += 1
                        res["msgs"].append(u"FAILED {} -> {}: {}".format(job["name"], tgt["name"], ex))
                for k in ("changed", "same", "fail", "skipped"):
                    res[k] += row[k]
                res["per_target"].append(row)
            if cleanup is not None:
                note = cleanup()
                if note:
                    res["msgs"].append(note)
            status = t.Commit()

        if status != DB.TransactionStatus.Committed:
            forms.alert(u"Revit rolled back the transaction ({}). No changes were saved."
                        .format(status), title="Visibility Manager")
            res["fail"] += res["changed"] + res["same"]
            res["changed"] = res["same"] = 0
            return res

        try:
            uidoc.RefreshActiveView()
        except Exception:
            pass

        for (vname, tname, key), n in sorted(skip_notes.items()):
            res["msgs"].append(u"SKIPPED {} item(s) on view '{}': its template '{}' controls "
                               u"'{}' - tick the template in [3] instead, or untick that row "
                               u"in the template's Include column.".format(n, vname, tname, key))
        for tname, note in sorted(na_notes):
            res["msgs"].append(u"SKIPPED on '{}': {}".format(tname, note))
        for tname, key in sorted(include_notes):
            res["msgs"].append(u"NOTE template '{}': '{}' is not included (Include unchecked), so "
                               u"the change is stored but won't affect views using it."
                               .format(tname, key))
        return res

    def _report_result(self, action, res):
        self.txtStatus.Text = (u"{}: {} changed, {} already identical (no change needed), "
                               u"{} failed, {} skipped/n.a.").format(
            action, res["changed"], res["same"], res["fail"], res["skipped"])

        output.print_md(u"### Visibility Manager - {}".format(action))
        output.print_md(u"**{} changed · {} already identical · {} failed · {} skipped "
                        u"(template-controlled)**".format(res["changed"], res["same"],
                                                          res["fail"], res["skipped"]))
        if res["per_target"]:
            safetable.print_table(output, 
                [[r["name"], r["changed"], r["same"], r["fail"], r["skipped"]] for r in res["per_target"]],
                columns=["Target", "Changed", "Already identical", "Failed", "Skipped"])
        if res["changed"] == 0 and res["same"] > 0 and res["fail"] == 0:
            output.print_md(u"> Nothing changed because the target(s) **already had exactly these "
                            u"settings** (e.g. the target is a duplicate of the source view). "
                            u"Change the source view first, or pick different targets.")
        for m in res["msgs"][:300]:
            output.print_md(u"- {}".format(m))
        if len(res["msgs"]) > 300:
            output.print_md(u"- ... and {} more".format(len(res["msgs"]) - 300))

    def _check_selection(self, verb):
        sources = self.get_selected_source()
        targets = self.get_selected_targets()
        if not sources:
            forms.alert(u"Select at least one {} to {}.".format(NOUN[self.mode], verb))
            return None, None
        if not targets:
            forms.alert("Tick at least one target view [2] or template [3].")
            return None, None
        return sources, targets

    # ------------------------------------------------------------- copy
    def _copy_job(self, src):
        """Copy + read-back verification. Returns "same" / "changed" /
        ("changed", note) and raises if the target doesn't end up matching."""
        key = self._source_key(src)
        sv = src["src_view"]
        obj = src["obj"]
        if self.mode == "filters":
            def fn(tv):
                before = filters_core.filter_snapshot(tv, obj)
                expected = filters_core.filter_snapshot(sv, obj)
                if before == expected:
                    return "same"
                filters_core.copy_filter_to_view(sv, tv, obj)
                after = filters_core.filter_snapshot(tv, obj)
                if after != expected:
                    raise RuntimeError(u"Revit didn't keep: " + filters_core.describe_diff(expected, after))
                note = ""
                sp, tp = filters_core.filter_position(sv, obj), filters_core.filter_position(tv, obj)
                if sp and tp and tp[0] > sp[0]:
                    note = (u"filter is #{} of {} in the target's priority order (#{} in source). "
                            u"Filters higher in the list win, so a filter above it may be masking "
                            u"its overrides - reorder in V/G > Filters if needed."
                            .format(tp[0] + 1, tp[1], sp[0] + 1))
                return ("changed", note)
        elif self.mode == "vg":
            def fn(tv):
                before = vg_core.category_snapshot(tv, obj)
                expected = vg_core.category_snapshot(sv, obj, applicability_view=tv)
                if before == expected:
                    return "same"
                vg_core.copy_category_override(sv, tv, obj)
                after = vg_core.category_snapshot(tv, obj)
                if after != expected:
                    raise RuntimeError("Revit didn't keep the override/visibility on this view")
                return "changed"
        elif self.mode == "worksets":
            fn = lambda tv: worksets_core.copy_workset(sv, tv, obj)
        else:
            return self._link_copy_jobs(src)
        return [{"name": src["name"], "key": key, "apply": fn}]

    def _copy_links_full_tab(self, sources, targets):
        """Revit-native copy of the whole Revit Links tab via a temporary
        template that controls only 'V/G Overrides RVT Links', followed by
        the per-instance Visibility/Halftone jobs for the ticked links
        (instance hides are element data that templates don't carry)."""
        src_view = sources[0]["src_view"]
        elem_src = sources[0]["elem_src"]
        all_link_ids = [lid for lid, _ in links_core.get_project_links(doc)]
        state = {"tmp": None}

        def prepare():
            state["tmp"] = links_core.build_links_only_template(src_view)

        def cleanup():
            if state["tmp"] is not None and not links_core.delete_temp_template(state["tmp"]):
                return (u"NOTE couldn't delete the temporary template '{}' - delete it "
                        u"manually.".format(state["tmp"].Name))
            return ""

        jobs = [{"name": u"Revit Links tab (all links)", "key": "links",
                 "apply": lambda tv: links_core.apply_links_tab(state["tmp"], src_view, tv, all_link_ids),
                 "na_note": ""}]
        for src in sources:
            obj = src["obj"]
            jobs.append({"name": src["name"] + u" [Visibility/Halftone]", "key": None,
                         "apply": (lambda o: lambda tv: links_core.copy_element_state(elem_src, tv, o))(obj),
                         "na_note": u"link Visibility/Halftone are per-view element settings - "
                                    u"view templates can't store them."})
        return self._run_batch("Copy Links Tab", targets, jobs, prepare=prepare, cleanup=cleanup)

    def _link_copy_jobs(self, src):
        """Two jobs per link, matching the two halves of the V/G Revit
        Links tab row:
          1. Display Settings (By Host / By Linked / Custom + all its
             sub-settings) - template-controllable ("links").
          2. Visibility checkbox + Halftone - per-view element state, never
             template-controlled, not possible on templates.
        """
        sv, ev, obj, name = src["src_view"], src["elem_src"], src["obj"], src["name"]

        def display(tv):
            expected = links_core.settings_snapshot(sv, obj)
            if links_core.settings_snapshot(tv, obj) == expected:
                return "same"
            links_core.copy_link_override(sv, tv, obj)
            after = links_core.settings_snapshot(tv, obj)
            if after != expected:
                raise RuntimeError(u"display settings differ after copy in: "
                                   + links_core.diff_settings(expected, after))
            return "changed"

        return [
            {"name": name + u" [Display Settings]", "key": "links", "apply": display},
            {"name": name + u" [Visibility/Halftone]", "key": None,
             "apply": lambda tv: links_core.copy_element_state(ev, tv, obj),
             "na_note": u"link Visibility/Halftone are per-view element settings - "
                        u"view templates can't store them (applied to views only)."},
        ]

    def OnCopyClick(self, sender, args):
        sources, targets = self._check_selection("copy")
        if not sources:
            return
        if not forms.alert(u"Copy {} {} to {} target(s)?".format(
                len(sources), NOUN_PL[self.mode], len(targets)), yes=True, no=True):
            return
        if self.mode == "links":
            choice = forms.alert(
                "How should Revit Links be copied?\n\n"
                "FULL TAB (recommended): Revit copies the entire Revit Links tab from the "
                "source view - Visibility, Halftone, Underlay and Display Settings including "
                "all Custom sub-tabs (Basics, Model Categories, Worksets, Filters...). "
                "Applies to ALL links; views only (not templates); same view type as source.\n\n"
                "SELECTED LINKS: only the ticked links, but only Visibility, By-element "
                "halftone and Display type / linked view (the API can't reach Halftone/"
                "Underlay columns or Custom sub-tabs).",
                options=["Full tab (all links)", "Selected links only"],
                title="Revit Links copy method")
            if not choice:
                return
            if choice.startswith("Full"):
                res = self._copy_links_full_tab(sources, targets)
                self.refresh_source_list()
                self._report_result("Copy (Revit Links - full tab)", res)
                return
        res = self._run_batch("Copy", targets, [j for s in sources for j in self._copy_job(s)])
        self.refresh_source_list()
        self._report_result("Copy", res)

    # ----------------------------------------------------------- remove
    def _remove_job(self, src):
        obj = src["obj"]
        if self.mode == "filters":
            def fn(tv):
                if not filters_core.remove_filter_from_view(tv, obj):
                    return "same"   # wasn't applied
                if filters_core.filter_snapshot(tv, obj) is not None:
                    raise RuntimeError("filter is still applied after RemoveFilter")
                return "changed"
        elif self.mode == "vg":
            def fn(tv):
                before = vg_core.category_snapshot(tv, obj)
                vg_core.reset_category_override(tv, obj)
                after = vg_core.category_snapshot(tv, obj)
                if after[0] is not None and filters_core.has_override(tv.GetCategoryOverrides(obj.Id)):
                    raise RuntimeError("override is still present after reset")
                return "same" if before == after else "changed"
        elif self.mode == "worksets":
            fn = lambda tv: worksets_core.reset_workset(tv, obj)
        else:
            def display(tv):
                if links_core.settings_snapshot(tv, obj) == ("ByHostView",):
                    return "same"
                links_core.reset_link_override(tv, obj)
                if links_core.settings_snapshot(tv, obj) != ("ByHostView",):
                    raise RuntimeError("link is not back to By Host View after reset")
                return "changed"
            return [
                {"name": src["name"] + u" [Display Settings]", "key": "links", "apply": display},
                {"name": src["name"] + u" [Visibility/Halftone]", "key": None,
                 "apply": lambda tv: links_core.reset_element_state(tv, obj),
                 "na_note": u"link Visibility/Halftone don't exist on view templates."},
            ]
        return [{"name": src["name"], "key": self._source_key(src), "apply": fn}]

    def OnRemoveClick(self, sender, args):
        sources, targets = self._check_selection("remove/reset")
        if not sources:
            return
        verb = {"filters": "remove", "vg": "reset overrides/visibility for",
                "links": "reset (By Host View, visible, no halftone)",
                "worksets": "reset to 'Using Global Setting'"}[self.mode]
        if not forms.alert(u"This will {} {} item(s) on {} target(s). Continue?".format(
                verb, len(sources), len(targets)), yes=True, no=True):
            return
        res = self._run_batch("Remove-Reset", targets, [j for s in sources for j in self._remove_job(s)])
        self.refresh_source_list()
        self._report_result("Remove/Reset", res)

    # ------------------------------------------------------------ export
    def OnExportCsvClick(self, sender, args):
        self._do_export(".csv")

    def OnExportXlsxClick(self, sender, args):
        self._do_export(".xlsx")

    def _do_export(self, ext):
        sources = self.get_selected_source()
        if not sources:
            forms.alert("Select at least one item to export.")
            return
        save_path = forms.save_file(file_ext=ext.lstrip("."))
        if not save_path:
            return
        if not save_path.lower().endswith(ext):
            save_path += ext

        rows = []
        ofields = list(filters_core.OVERRIDE_FIELDS)
        if self.mode == "filters":
            fieldnames = ["Name", "Status", "Visible"] + ofields
            for src in sources:
                sv, fid = src["src_view"], src["obj"]
                try:
                    enabled = sv.GetIsFilterEnabled(fid)
                except Exception:
                    enabled = True
                row = {"Name": src["name"], "Status": "Enabled" if enabled else "Disabled",
                       "Visible": "Yes" if sv.GetFilterVisibility(fid) else "No"}
                row.update(filters_core.override_to_dict(doc, sv.GetFilterOverrides(fid)))
                rows.append(row)
        elif self.mode == "vg":
            fieldnames = ["Category", "Status", "Visible"] + ofields
            for src in sources:
                ogs, hidden = vg_core.get_category_state(src["src_view"], src["obj"])
                row = {"Category": src["name"],
                       "Status": "Override" if filters_core.has_override(ogs) else "",
                       "Visible": "No" if hidden else "Yes"}
                row.update(filters_core.override_to_dict(doc, ogs))
                rows.append(row)
        elif self.mode == "links":
            fieldnames = ["Name", "Status", "Visible", "Halftone", "VisibilityType", "LinkedViewName"]
            for src in sources:
                st = links_core.element_state(src["elem_src"], src["obj"])
                row = {"Name": src["name"], "Status": src["status"],
                       "Visible": "" if st is None else ("No" if (st[0] or st[1]) else "Yes"),
                       "Halftone": "" if st is None or st[2] is None
                       else dict(st[2]).get("Halftone", "No")}
                row.update(links_core.override_to_dict(src["src_view"], src["obj"]))
                rows.append(row)
        else:
            fieldnames = ["Name", "Visibility"]
            for src in sources:
                rows.append({"Name": src["name"], "Visibility": src["status"]})

        try:
            actual_path, used_xlsx = io_utils.export_rows(save_path, fieldnames, rows)
        except Exception as ex:
            forms.alert(u"Export failed: {}".format(ex))
            return

        noun = {"filters": "Filters", "vg": "V/G Overrides", "links": "Revit Links",
                "worksets": "Worksets"}[self.mode]
        msg = u"{} exported.\n\nFile: {}\n{} row(s).".format(noun, os.path.basename(actual_path), len(rows))
        if ext == ".xlsx" and not used_xlsx:
            msg += "\n\n(openpyxl not available in this engine - saved as CSV instead.)"
        self.txtStatus.Text = u"Export complete: {} row(s).".format(len(rows))
        forms.alert(msg, title=u"{} Export Complete".format(noun))

    # ------------------------------------------------------------ import
    def _import_jobs(self, rows):
        jobs, errors = [], []
        name_key = "Category" if self.mode == "vg" else "Name"

        if self.mode == "filters":
            by_name = dict((n, fid) for fid, n in filters_core.get_all_project_filters(doc))
        elif self.mode == "vg":
            by_name = dict((c["name"], c) for c in vg_core.get_categories(doc, self.active_view))
        elif self.mode == "links":
            by_name = dict((n, lid) for lid, n in links_core.get_project_links(doc))
        else:
            by_name = dict((n, wid) for wid, n in worksets_core.get_worksets(doc))

        for row in rows:
            name = row.get(name_key, "")
            if not name:
                continue
            if name not in by_name:
                errors.append(u"NOT FOUND {} '{}' in this project.".format(NOUN[self.mode], name))
                continue
            if self.mode == "filters":
                fid = by_name[name]
                ogs = filters_core.dict_to_override(doc, row)
                enabled = filters_core._to_bool(row.get("Status"))
                visible = filters_core._to_bool(row.get("Visible"))
                fn = (lambda f, o, e, v: lambda tv: filters_core.apply_filter_state(tv, f, o, e, v))(
                    fid, ogs, enabled, visible)
                key = "filters"
            elif self.mode == "vg":
                c = by_name[name]
                ogs = filters_core.dict_to_override(doc, row)
                vis = filters_core._to_bool(row.get("Visible"))
                if vis is None:  # legacy files: Status == "Hidden"
                    hidden = str(row.get("Status", "")).strip().lower().startswith("hidden")
                else:
                    hidden = not vis
                fn = (lambda cat, o, h: lambda tv: vg_core.apply_category_state(tv, cat, o, h))(
                    c["cat"], ogs, hidden)
                key = c["template_key"]
            elif self.mode == "links":
                lid = by_name[name]
                vis = filters_core._to_bool(row.get("Visible"))
                half = filters_core._to_bool(row.get("Halftone"))

                def display(tv, l=lid, r=row):
                    before = links_core.settings_snapshot(tv, l)
                    links_core.dict_to_override_and_apply(tv, l, r)
                    return "same" if links_core.settings_snapshot(tv, l) == before else "changed"
                jobs.append({"name": name + u" [Display Settings]", "key": "links", "apply": display})
                if vis is not None or half is not None:
                    jobs.append({"name": name + u" [Visibility/Halftone]", "key": None,
                                 "apply": (lambda l, v, h: lambda tv:
                                           links_core.apply_element_row(tv, l, v, h))(lid, vis, half),
                                 "na_note": u"link Visibility/Halftone don't exist on view templates."})
                continue
            else:
                wid = by_name[name]
                jobs.append({"name": name, "key": "worksets",
                             "apply": (lambda w, t: lambda tv: worksets_core.apply_row(tv, w, t))(
                                 wid, row.get("Visibility"))})
                continue
            jobs.append({"name": name, "key": key, "apply": self._with_change_check(fn, name)})
        return jobs, errors

    def _with_change_check(self, fn, name):
        """Wrap an import apply() so it reports "same" when the target
        already had these settings (compared by read-back snapshot)."""
        mode = self.mode
        if mode == "filters":
            fid = dict((n, f) for f, n in filters_core.get_all_project_filters(doc)).get(name)
            snap = lambda tv: filters_core.filter_snapshot(tv, fid)
        elif mode == "vg":
            cat = dict((c["name"], c["cat"]) for c in vg_core.get_categories(doc, self.active_view)).get(name)
            snap = lambda tv: vg_core.category_snapshot(tv, cat)
        else:
            return fn

        def wrapped(tv):
            before = snap(tv)
            fn(tv)
            return "same" if snap(tv) == before else "changed"
        return wrapped

    def OnImportClick(self, sender, args):
        targets = self.get_selected_targets()
        if not targets:
            forms.alert("Tick at least one target view [2] or template [3] to import into.")
            return
        # FIX: file_ext="xlsx|csv" produced an invalid dialog filter.
        pick_path = forms.pick_file(files_filter="CSV / Excel (*.csv;*.xlsx)|*.csv;*.xlsx")
        if not pick_path:
            return
        try:
            rows = io_utils.import_rows(pick_path)
        except Exception as ex:
            forms.alert(u"Import failed: {}".format(ex))
            return
        if not rows:
            forms.alert("No rows found in file.")
            return

        jobs, errors = self._import_jobs(rows)
        if not jobs:
            self._report_result("Import", {"changed": 0, "same": 0, "fail": len(errors),
                                           "skipped": 0, "msgs": errors, "per_target": []})
            forms.alert("Nothing in the file matched this project in {} mode.".format(self.mode))
            return
        if not forms.alert(u"Apply {} row(s) to {} target(s)?".format(len(jobs), len(targets)),
                           yes=True, no=True):
            return
        res = self._run_batch("Import", targets, jobs)
        res["fail"] += len(errors)
        res["msgs"] = errors + res["msgs"]
        self.refresh_source_list()
        self._report_result("Import", res)

    # ------------------------------------------------------------- close
    def OnCloseClick(self, sender, args):
        self.Close()


if __name__ == "__main__":
    xaml_path = os.path.join(os.path.dirname(__file__), "VisManagerUI.xaml")
    VisManagerWindow(xaml_path).ShowDialog()
