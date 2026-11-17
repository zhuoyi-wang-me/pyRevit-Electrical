# -*- coding: utf-8 -*-
"""
Cable Tray Tag Manager - pyRevit
- Modeless WPF window: pan/zoom freely while UI is open
- Hide -> PickObjects -> Show pattern for Revit interaction
- All workflow logic preserved
"""
import clr
import os
import io
import tempfile

clr.AddReference("RevitAPI")
clr.AddReference("RevitAPIUI")
from Autodesk.Revit.DB import *
from Autodesk.Revit.UI import *
from Autodesk.Revit.UI.Selection import *
from pyrevit import forms

# ---------------------------------------------------------------------------
# DOCUMENT ACCESS
# ---------------------------------------------------------------------------
try:
    uidoc = __revit__.ActiveUIDocument
    doc   = uidoc.Document
except:
    from pyrevit import HOST_APP
    uidoc = HOST_APP.uidoc
    doc   = HOST_APP.doc

# ---------------------------------------------------------------------------
# SELECTION FILTER
# ---------------------------------------------------------------------------

def _id_int(element_id):
    """ElementId -> int in every Revit version: .Value (2024+) - .IntegerValue was REMOVED in Revit 2026."""
    value = getattr(element_id, 'Value', None)
    return int(element_id.IntegerValue if value is None else value)


class TrayAndTagFilter(ISelectionFilter):
    def AllowElement(self, el):
        try:
            cat_id = _id_int(el.Category.Id)
            return cat_id in [
                int(BuiltInCategory.OST_CableTrayTags),
                int(BuiltInCategory.OST_MultiCategoryTags),
                int(BuiltInCategory.OST_CableTray)
            ]
        except:
            return False
    def AllowReference(self, ref, point):
        return False


# ---------------------------------------------------------------------------
# Check if a tray already has a CableTray/MultiCategory tag in the active view
# ---------------------------------------------------------------------------
def tray_already_tagged(tray, view_id):
    """Return True if any IndependentTag in the active view already tags this tray."""
    try:
        tag_collector = (
            FilteredElementCollector(doc, view_id)
            .OfClass(IndependentTag)
            .WhereElementIsNotElementType()
        )
        for tag in tag_collector:
            try:
                cat_id = _id_int(tag.Category.Id)
                if cat_id not in [int(BuiltInCategory.OST_CableTrayTags),
                                  int(BuiltInCategory.OST_MultiCategoryTags)]:
                    continue
                for tagged_ref in tag.GetTaggedReferences():
                    if tagged_ref.ElementId == tray.Id:
                        return True
            except:
                continue
    except:
        pass
    return False

# ---------------------------------------------------------------------------
# XAML  (no changes to layout)
# ---------------------------------------------------------------------------
xaml_content = """
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="Cable Tray Tag Manager"
        Height="500" Width="420"
        WindowStartupLocation="CenterScreen"
        Topmost="True"
        ResizeMode="NoResize">
    <StackPanel Margin="20">
        <TextBlock Text="Update Tray Tag:" FontWeight="Bold" Margin="0,0,0,10"/>

        <TextBlock Text="Leader Mode:"/>
        <ComboBox Name="cbLeader" Margin="0,5,0,10" SelectionChanged="OnLeaderChanged">
            <ComboBoxItem Content="No Leader" IsSelected="True"/>
            <ComboBoxItem Content="Attached"/>
            <ComboBoxItem Content="Free"/>
        </ComboBox>

        <TextBlock Text="Head Y Spacing (mm):"/>
        <Grid Margin="0,5,0,10">
            <Grid.ColumnDefinitions>
                <ColumnDefinition Width="*"/>
                <ColumnDefinition Width="30"/>
                <ColumnDefinition Width="30"/>
                <ColumnDefinition Width="60"/>
                <ColumnDefinition Width="35"/>
            </Grid.ColumnDefinitions>
            <TextBox Name="txtHeadY" Text="400" VerticalContentAlignment="Center"/>
            <Button Name="btnUpHeadY"   Grid.Column="1" Content="+" Click="AdjustValue" Tag="50"/>
            <Button Name="btnDnHeadY"   Grid.Column="2" Content="-" Click="AdjustValue" Tag="-50"/>
            <CheckBox Name="chkNegHeadY" Grid.Column="3" Content="Neg" Margin="5,0,0,0" VerticalAlignment="Center"/>
            <Button Name="btnClearHeadY" Grid.Column="4" Content="0"  Margin="5,0,0,0" Click="ClearValue"/>
        </Grid>

        <TextBlock Text="Elbow X Spacing (mm):" Name="lblElbowX"/>
        <Grid Name="gridElbowX" Margin="0,5,0,10">
            <Grid.ColumnDefinitions>
                <ColumnDefinition Width="*"/>
                <ColumnDefinition Width="30"/>
                <ColumnDefinition Width="30"/>
                <ColumnDefinition Width="60"/>
                <ColumnDefinition Width="35"/>
            </Grid.ColumnDefinitions>
            <TextBox Name="txtElbowX" Text="0" VerticalContentAlignment="Center"/>
            <Button Name="btnUpElbowX"   Grid.Column="1" Content="+" Click="AdjustValue" Tag="50"/>
            <Button Name="btnDnElbowX"   Grid.Column="2" Content="-" Click="AdjustValue" Tag="-50"/>
            <CheckBox Name="chkNegElbowX" Grid.Column="3" Content="Neg" Margin="5,0,0,0" VerticalAlignment="Center"/>
            <Button Name="btnClearElbowX" Grid.Column="4" Content="0"  Margin="5,0,0,0" Click="ClearValue"/>
        </Grid>

        <TextBlock Text="Elbow Y Spacing (mm):" Name="lblElbowY"/>
        <Grid Name="gridElbowY" Margin="0,5,0,20">
            <Grid.ColumnDefinitions>
                <ColumnDefinition Width="*"/>
                <ColumnDefinition Width="30"/>
                <ColumnDefinition Width="30"/>
                <ColumnDefinition Width="60"/>
                <ColumnDefinition Width="35"/>
            </Grid.ColumnDefinitions>
            <TextBox Name="txtElbowY" Text="400" VerticalContentAlignment="Center"/>
            <Button Name="btnUpElbowY"   Grid.Column="1" Content="+" Click="AdjustValue" Tag="50"/>
            <Button Name="btnDnElbowY"   Grid.Column="2" Content="-" Click="AdjustValue" Tag="-50"/>
            <CheckBox Name="chkNegElbowY" Grid.Column="3" Content="Neg" Margin="5,0,0,0" VerticalAlignment="Center"/>
            <Button Name="btnClearElbowY" Grid.Column="4" Content="0"  Margin="5,0,0,0" Click="ClearValue"/>
        </Grid>

        <Button Name="btnRun"
                Content="SELECT TAGS &amp; TRAYS"
                Height="50"
                Background="#0078D7"
                Foreground="White"
                FontWeight="Bold"
                Click="btnRun_Click"/>

        <Button Name="btnReapply"
                Content="RE-APPLY TO LAST SELECTION"
                Height="40"
                Margin="0,8,0,0"
                Background="#5C6BC0"
                Foreground="White"
                FontWeight="Bold"
                IsEnabled="False"
                Click="btnReapply_Click"/>

        <TextBlock Name="lblStatus"
                   Text="Ready - pan/zoom freely, then click SELECT."
                   Margin="0,12,0,0"
                   TextWrapping="Wrap"
                   Foreground="#555555"
                   FontStyle="Italic"/>
    </StackPanel>
</Window>
"""

temp_xaml = os.path.join(tempfile.gettempdir(), 'tag_aligner_modeless.xaml')
with io.open(temp_xaml, 'w', encoding='utf-8') as f:
    f.write(xaml_content)

# ---------------------------------------------------------------------------
# WPF WINDOW  - modeless
# ---------------------------------------------------------------------------
class TagElbowTool(forms.WPFWindow):
    def __init__(self, xaml_path):
        forms.WPFWindow.__init__(self, xaml_path)
        self._update_ui("No Leader")
        # Cache of last successful selection - lets the user re-apply
        # with new spacing/leader settings without re-picking elements
        self._last_master_tag = None
        self._last_trays = None
        # Tracks tag ElementIds created by the last run, keyed by tray ElementId.
        # Re-apply deletes these before creating new tags (re-tag behavior).
        self._last_created_tags = {}

    # ------------------------------------------------------------------
    # UI STATE
    # ------------------------------------------------------------------
    def _update_ui(self, mode):
        try:
            self.gridElbowX.IsEnabled = (mode != "No Leader")
            self.gridElbowX.Opacity   = 1.0 if mode != "No Leader" else 0.5
            self.gridElbowY.IsEnabled = (mode == "Free")
            self.gridElbowY.Opacity   = 1.0 if mode == "Free" else 0.5
        except:
            pass

    def _set_status(self, msg, color="#555555"):
        try:
            from System.Windows.Media import BrushConverter
            self.lblStatus.Text       = msg
            self.lblStatus.Foreground = BrushConverter().ConvertFromString(color)
        except:
            pass

    # ------------------------------------------------------------------
    # XAML EVENT HANDLERS
    # ------------------------------------------------------------------
    def OnLeaderChanged(self, sender, e):
        try:
            self._update_ui(sender.SelectedItem.Content)
        except:
            pass

    def AdjustValue(self, sender, e):
        mapping = {
            "btnUpHeadY":  self.txtHeadY,
            "btnDnHeadY":  self.txtHeadY,
            "btnUpElbowX": self.txtElbowX,
            "btnDnElbowX": self.txtElbowX,
            "btnUpElbowY": self.txtElbowY,
            "btnDnElbowY": self.txtElbowY,
        }
        target = mapping.get(sender.Name)
        if target:
            try:
                target.Text = str(int(float(target.Text) + float(sender.Tag)))
            except:
                target.Text = "0"

    def ClearValue(self, sender, e):
        if   "HeadY"  in sender.Name: self.txtHeadY.Text  = "0"
        elif "ElbowX" in sender.Name: self.txtElbowX.Text = "0"
        elif "ElbowY" in sender.Name: self.txtElbowY.Text = "0"

    def btnRun_Click(self, sender, e):
        """
        MODELESS pattern:
          1. Hide window  -> Revit gets focus, user can pick
          2. PickObjects  -> runs on Revit UI thread (now free)
          3. Process      -> transaction inside Revit context
          4. Show window  -> return to modeless state
        """
        self.Hide()

        try:
            # --- PICK ---
            try:
                selected_refs = uidoc.Selection.PickObjects(
                    ObjectType.Element,
                    TrayAndTagFilter(),
                    "Select ONE Master Tag + Trays, then click FINISH"
                )
            except:
                # ESC - just restore the window
                self._set_status("Selection cancelled.", "#888888")
                self.Show()
                self.Activate()
                return

            # --- PARSE SELECTION ---
            master_tag = None
            trays      = []
            for ref in selected_refs:
                el     = doc.GetElement(ref)
                cat_id = _id_int(el.Category.Id)
                if cat_id in [int(BuiltInCategory.OST_CableTrayTags),
                               int(BuiltInCategory.OST_MultiCategoryTags)]:
                    if master_tag is None:
                        master_tag = el
                elif cat_id == int(BuiltInCategory.OST_CableTray):
                    trays.append(el)

            if not master_tag or not trays:
                self._set_status("Need 1 tag + at least 1 tray. Try again.", "#CC0000")
                self.Show()
                self.Activate()
                return

            # Cache this selection so "Re-apply" can reuse it without re-picking
            self._last_master_tag = master_tag
            self._last_trays = trays
            try:
                self.btnReapply.IsEnabled = True
            except:
                pass

            # Fresh selection - reset created-tags cache before tagging
            self._last_created_tags = {}
            self._process_tags(master_tag, trays, retag=False)

        except Exception as ex:
            self._set_status("Error: " + str(ex), "#CC0000")
            self.Show()
            self.Activate()

    def btnReapply_Click(self, sender, e):
        """
        Re-run tagging using the LAST selected master tag + trays,
        with whatever settings are currently in the form.
        No PickObjects call - no need to reselect anything.
        """
        if not self._last_master_tag or not self._last_trays:
            self._set_status("No previous selection cached. Use SELECT first.", "#CC0000")
            return

        # Validate cached elements still exist in the document
        try:
            master_tag = doc.GetElement(self._last_master_tag.Id)
            trays = []
            for tray in self._last_trays:
                t_check = doc.GetElement(tray.Id)
                if t_check:
                    trays.append(t_check)
        except:
            master_tag = None
            trays = []

        if not master_tag or not trays:
            self._set_status(
                "Cached selection is no longer valid (elements deleted?). Use SELECT again.",
                "#CC0000"
            )
            self._last_master_tag = None
            self._last_trays = None
            try:
                self.btnReapply.IsEnabled = False
            except:
                pass
            return

        self.Hide()
        try:
            self._process_tags(master_tag, trays, retag=True)
        except Exception as ex:
            self._set_status("Error: " + str(ex), "#CC0000")
            self.Show()
            self.Activate()

    def _process_tags(self, master_tag, trays, retag=False):
        """
        Shared logic: optionally delete previously-created tags (retag mode),
        filter out trays still tagged by OTHER tags, then create new tags
        with current form settings. Called by both SELECT and RE-APPLY.
        Always restores (Show/Activate) the form before returning.

        retag=True (Re-apply path):
            - Deletes tags this tool created on the previous run
              (tracked in self._last_created_tags) before re-evaluating
              which trays still need tagging.
        """
        # --- snapshot settings ---
        try:
            h_y_mm    = float(self.txtHeadY.Text)
            e_x_mm    = float(self.txtElbowX.Text)
            e_y_mm    = float(self.txtElbowY.Text)
            neg_hy    = bool(self.chkNegHeadY.IsChecked)
            neg_ex    = bool(self.chkNegElbowX.IsChecked)
            neg_ey    = bool(self.chkNegElbowY.IsChecked)
            leader_mode = str(self.cbLeader.SelectedItem.Content)
        except Exception as ex:
            self._set_status("Error reading values: " + str(ex), "#CC0000")
            self.Show()
            self.Activate()
            return

        try:
            ft = 1.0 / 304.8
            h_y = h_y_mm * ft * (-1 if neg_hy else 1)
            e_x = e_x_mm * ft * (-1 if neg_ex else 1)
            e_y = e_y_mm * ft * (-1 if neg_ey else 1)

            has_leader  = leader_mode != "No Leader"
            is_attached = "Attached" in leader_mode

            master_head  = master_tag.TagHeadPosition
            master_elbow = master_head
            try:
                tagged_ref   = master_tag.GetTaggedReferences()[0]
                master_elbow = master_tag.GetLeaderElbow(tagged_ref)
            except:
                pass

            view_id = doc.ActiveView.Id
            deleted_count = 0

            with Transaction(doc, "CodeX: Align Tags") as t:
                t.Start()

                # --- RETAG: delete tags created by the previous run ---
                if retag and self._last_created_tags:
                    for tray_id_int, old_tag_id in list(self._last_created_tags.items()):
                        try:
                            old_tag = doc.GetElement(old_tag_id)
                            if old_tag:
                                doc.Delete(old_tag_id)
                                deleted_count += 1
                        except:
                            pass
                    self._last_created_tags = {}
                    doc.Regenerate()

                # --- FILTER OUT TRAYS STILL TAGGED BY ANOTHER TAG ---
                untagged_trays = []
                skipped_count = 0
                for tray in trays:
                    if tray_already_tagged(tray, view_id):
                        skipped_count += 1
                    else:
                        untagged_trays.append(tray)

                if not untagged_trays:
                    t.RollBack() if deleted_count == 0 else t.Commit()
                    msg = "All {0} tray(s) already tagged. Nothing to create.".format(skipped_count)
                    if deleted_count:
                        msg = "Deleted {0} old tag(s). ".format(deleted_count) + msg
                    self._set_status(msg, "#CC8800")
                    self.Show()
                    self.Activate()
                    return

                trays_to_tag = sorted(
                    untagged_trays,
                    key=lambda x: x.Location.Curve.GetEndPoint(0).Y,
                    reverse=(h_y < 0)
                )

                new_created_tags = {}
                for i, tray in enumerate(trays_to_tag, 1):
                    new_head = XYZ(
                        master_head.X,
                        master_head.Y + (h_y * i),
                        master_head.Z
                    )
                    new_tag = IndependentTag.Create(
                        doc,
                        master_tag.GetTypeId(),
                        doc.ActiveView.Id,
                        Reference(tray),
                        has_leader,
                        master_tag.TagOrientation,
                        new_head
                    )
                    doc.Regenerate()

                    if has_leader:
                        new_tag.LeaderEndCondition = (
                            LeaderEndCondition.Attached
                            if is_attached else
                            LeaderEndCondition.Free
                        )
                        doc.Regenerate()
                        new_tag.TagHeadPosition = new_head

                        y_target  = (new_tag.TagHeadPosition.Y
                                     if is_attached else
                                     master_elbow.Y + (e_y * i))
                        new_elbow = XYZ(
                            master_elbow.X + (e_x * i),
                            y_target,
                            master_elbow.Z
                        )
                        proj          = tray.Location.Curve.Project(new_elbow)
                        new_end_point = (proj.XYZPoint
                                         if proj else
                                         tray.Location.Curve.GetEndPoint(0))
                        if not is_attached:
                            new_tag.SetLeaderEnd(Reference(tray), new_end_point)
                        try:
                            new_tag.SetLeaderElbow(Reference(tray), new_elbow)
                        except:
                            pass

                    # Track tray.Id -> new_tag.Id for future retag/delete
                    new_created_tags[_id_int(tray.Id)] = new_tag.Id

                t.Commit()

            # Merge into the cache (covers case where retag only re-tagged a subset)
            self._last_created_tags.update(new_created_tags)

            done_msg = "Done - {0} tag(s) created.".format(len(trays_to_tag))
            if deleted_count:
                done_msg = "Deleted {0} old tag(s). ".format(deleted_count) + done_msg
            if skipped_count > 0:
                done_msg += " ({0} tray(s) skipped - already tagged by another tag).".format(skipped_count)
            self._set_status(done_msg, "#007700")

        except Exception as ex:
            self._set_status("Error: " + str(ex), "#CC0000")

        finally:
            self.Show()
            self.Activate()

# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# show_dialog() in pyRevit calls WPF ShowDialog() internally - MODAL/blocking.
# For modeless (pan/zoom while open) we call the underlying WPF .Show() which
# is non-blocking, then keep the script alive with a dispatcher frame pump.
# ---------------------------------------------------------------------------
form = TagElbowTool(temp_xaml)

try:
    # Preferred: use WPF dispatcher frame to stay alive without blocking Revit
    from System.Windows.Threading import DispatcherFrame, Dispatcher
    from System.Windows import Application as WpfApp

    frame = DispatcherFrame()

    def on_closed(s, e):
        frame.Continue = False

    form.Closed += on_closed
    form.Show()

    # Pump the WPF dispatcher - keeps the window alive and responsive
    # while returning the Revit UI thread between events
    Dispatcher.PushFrame(frame)

except Exception as ex:
    # Fallback: show_dialog (modal) if dispatcher pump fails
    form.show_dialog()
