# -*- coding: utf-8 -*-
"""Generate one or more connected MEP routes (Cable Tray, Conduit, Duct,
or Pipe) from selected detail lines, arranged as a bank of parallel runs
spaced horizontally and/or vertically.

Features:
    - Single unified WPF dialog to configure all parameters on one screen.
    - Miter-vertex offset logic for clean automatic elbow/fitting placement at 90-degree turns.
    - Edge-to-edge clearance spacing (automatically adjusts centerline offsets based on tray sizes).
    - Individual tray type and size configuration per grid position.
    - Per-position Include checkbox so only a selected subset of the H x V
      grid actually gets modeled (with Select All / Clear All shortcuts),
      while unmodeled positions still contribute to spacing math so the
      trays you do keep land at their correct offsets.
"""

__title__ = "Generate\nMEP Route Bank"
__author__ = 'BHEEM'

import math
from collections import defaultdict, OrderedDict

import clr
clr.AddReference('PresentationFramework')
clr.AddReference('PresentationCore')
clr.AddReference('WindowsBase')

from System.Windows.Markup import XamlReader
from System.Windows import (
    Window, MessageBox, Thickness, CornerRadius, GridLength, 
    FontWeight, FontWeights, VerticalAlignment, HorizontalAlignment, TextWrapping,
    Visibility
)
from System.Windows.Controls import (
    StackPanel, Grid, TextBlock, TextBox, ComboBox, Button, Border, 
    RowDefinition, ColumnDefinition, CheckBox
)
from System.Windows.Media import Brushes, Color, SolidColorBrush

from pyrevit import revit, DB, UI, forms, script

doc = revit.doc
uidoc = revit.uidoc
output = script.get_output()
logger = script.get_logger()

# --------------------------------------------------------------------------
# Tunables & Units
# --------------------------------------------------------------------------
NODE_TOLERANCE_FT = 0.01          # ~3 mm
PARALLEL_TOLERANCE_DEG = 2.0      # angular tolerance for collinearity


def mm_to_internal(value_mm):
    try:
        return DB.UnitUtils.ConvertToInternalUnits(value_mm, DB.UnitTypeId.Millimeters)
    except AttributeError:
        return DB.UnitUtils.ConvertToInternalUnits(value_mm, DB.DisplayUnitType.DUT_MILLIMETERS)


def internal_to_mm(value_ft):
    try:
        return DB.UnitUtils.ConvertFromInternalUnits(value_ft, DB.UnitTypeId.Millimeters)
    except AttributeError:
        return DB.UnitUtils.ConvertFromInternalUnits(value_ft, DB.DisplayUnitType.DUT_MILLIMETERS)


def format_length_mm(length_ft):
    return "{:,.0f} mm".format(internal_to_mm(length_ft))


def hex_brush(hex_str):
    hex_str = hex_str.lstrip("#")
    r = int(hex_str[0:2], 16)
    g = int(hex_str[2:4], 16)
    b = int(hex_str[4:6], 16)
    return SolidColorBrush(Color.FromRgb(r, g, b))


TIER_ACCENT_PALETTE = ["#2563EB", "#7C3AED", "#059669", "#D97706", "#DC2626", "#0891B2"]


def safe_name(element):
    try:
        name = element.Name
        if name:
            return name
    except Exception:
        pass

    for bip in (DB.BuiltInParameter.SYMBOL_NAME_PARAM, DB.BuiltInParameter.ALL_MODEL_TYPE_NAME):
        try:
            param = element.get_Parameter(bip)
            if param and param.HasValue:
                value = param.AsString()
                if value:
                    return value
        except Exception:
            pass

    return "Unnamed ({0})".format(element.Id)


def type_name(element_type):
    try:
        family_name = element_type.FamilyName
    except Exception:
        family_name = None
    name = safe_name(element_type)
    if family_name:
        return family_name + " : " + name
    return name


def set_param(element, bip, value):
    try:
        param = element.get_Parameter(bip)
    except Exception:
        return
    if param and not param.IsReadOnly:
        param.Set(value)


def get_dim_mm(size_dict, key):
    if key == "width":
        return size_dict.get("width_mm", size_dict.get("diameter_mm", 0.0))
    elif key == "height":
        return size_dict.get("height_mm", size_dict.get("diameter_mm", 0.0))
    return 0.0


# --------------------------------------------------------------------------
# Category Configuration
# --------------------------------------------------------------------------
def create_cable_tray(document, type_id, system_type_id, p1, p2, level_id):
    return DB.Electrical.CableTray.Create(document, type_id, p1, p2, level_id)


def create_conduit(document, type_id, system_type_id, p1, p2, level_id):
    return DB.Electrical.Conduit.Create(document, type_id, p1, p2, level_id)


def create_duct(document, type_id, system_type_id, p1, p2, level_id):
    return DB.Mechanical.Duct.Create(document, system_type_id, type_id, level_id, p1, p2)


def create_pipe(document, type_id, system_type_id, p1, p2, level_id):
    return DB.Plumbing.Pipe.Create(document, system_type_id, type_id, level_id, p1, p2)


CATEGORY_CONFIG = OrderedDict()
CATEGORY_CONFIG["Cable Tray"] = dict(
    type_class=DB.Electrical.CableTrayType,
    instance_class=DB.Electrical.CableTray,
    needs_system_type=False,
    create=create_cable_tray,
    size_mode="rect",
    width_param=DB.BuiltInParameter.RBS_CABLETRAY_WIDTH_PARAM,
    height_param=DB.BuiltInParameter.RBS_CABLETRAY_HEIGHT_PARAM,
)
CATEGORY_CONFIG["Conduit"] = dict(
    type_class=DB.Electrical.ConduitType,
    instance_class=DB.Electrical.Conduit,
    needs_system_type=False,
    create=create_conduit,
    size_mode="round",
    diameter_param=DB.BuiltInParameter.RBS_CONDUIT_DIAMETER_PARAM,
)
CATEGORY_CONFIG["Duct"] = dict(
    type_class=DB.Mechanical.DuctType,
    instance_class=DB.Mechanical.Duct,
    needs_system_type=True,
    system_type_class=DB.MEPSystemType,
    system_type_category=DB.BuiltInCategory.OST_DuctSystem,
    create=create_duct,
    size_mode="auto",
    width_param=DB.BuiltInParameter.RBS_CURVE_WIDTH_PARAM,
    height_param=DB.BuiltInParameter.RBS_CURVE_HEIGHT_PARAM,
    diameter_param=DB.BuiltInParameter.RBS_CURVE_DIAMETER_PARAM,
)
CATEGORY_CONFIG["Pipe"] = dict(
    type_class=DB.Plumbing.PipeType,
    instance_class=DB.Plumbing.Pipe,
    needs_system_type=True,
    system_type_class=DB.MEPSystemType,
    system_type_category=DB.BuiltInCategory.OST_PipingSystem,
    create=create_pipe,
    size_mode="round",
    diameter_param=DB.BuiltInParameter.RBS_PIPE_DIAMETER_PARAM,
)


def format_size(size_values):
    if "diameter_mm" in size_values:
        return "{0:.0f} mm diameter".format(size_values["diameter_mm"])
    return "{0:.0f} mm x {1:.0f} mm".format(size_values["width_mm"], size_values["height_mm"])


def apply_size(cfg, element, size_values):
    if "width_mm" in size_values and "width_param" in cfg:
        set_param(element, cfg["width_param"], mm_to_internal(size_values["width_mm"]))
    if "height_mm" in size_values and "height_param" in cfg:
        set_param(element, cfg["height_param"], mm_to_internal(size_values["height_mm"]))
    if "diameter_mm" in size_values and "diameter_param" in cfg:
        set_param(element, cfg["diameter_param"], mm_to_internal(size_values["diameter_mm"]))


def read_size_from_element(cfg, element):
    """Reverse of apply_size: read the real width/height or diameter off an
    already-placed routing element, in mm. Tries diameter first (covers
    round Conduit/Pipe and round Duct), then falls back to width/height
    (covers Cable Tray and rectangular Duct)."""
    if "diameter_param" in cfg:
        try:
            p = element.get_Parameter(cfg["diameter_param"])
            if p and p.HasValue and p.AsDouble() > 1e-9:
                return {"diameter_mm": internal_to_mm(p.AsDouble())}
        except Exception:
            pass
    if "width_param" in cfg and "height_param" in cfg:
        try:
            pw = element.get_Parameter(cfg["width_param"])
            ph = element.get_Parameter(cfg["height_param"])
            if pw and ph and pw.HasValue and ph.HasValue and pw.AsDouble() > 1e-9:
                return {
                    "width_mm": internal_to_mm(pw.AsDouble()),
                    "height_mm": internal_to_mm(ph.AsDouble()),
                }
        except Exception:
            pass
    return {}




# --------------------------------------------------------------------------
# Unified Single WPF Dialog Configuration Window
# --------------------------------------------------------------------------
XAML_INTERFACE = """
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="MEP Route Bank Manager" Height="800" Width="760"
        WindowStartupLocation="CenterScreen" Background="#EEF1F5"
        FontFamily="Segoe UI">
    <Window.Resources>

        <!-- Design tokens -->
        <SolidColorBrush x:Key="AccentBrush" Color="#2563EB"/>
        <SolidColorBrush x:Key="AccentHoverBrush" Color="#1D4ED8"/>
        <SolidColorBrush x:Key="AccentPressedBrush" Color="#1E40AF"/>
        <SolidColorBrush x:Key="TextPrimaryBrush" Color="#111827"/>
        <SolidColorBrush x:Key="TextSecondaryBrush" Color="#6B7280"/>
        <SolidColorBrush x:Key="BorderBrushLight" Color="#E2E8F0"/>
        <SolidColorBrush x:Key="CardBrush" Color="#FFFFFF"/>
        <SolidColorBrush x:Key="GhostBrush" Color="#F1F5F9"/>
        <SolidColorBrush x:Key="GhostHoverBrush" Color="#E2E8F0"/>

        <!-- Rounded, focus-aware TextBox -->
        <Style TargetType="TextBox">
            <Setter Property="Padding" Value="8,4"/>
            <Setter Property="Height" Value="32"/>
            <Setter Property="FontSize" Value="13"/>
            <Setter Property="VerticalContentAlignment" Value="Center"/>
            <Setter Property="BorderBrush" Value="{StaticResource BorderBrushLight}"/>
            <Setter Property="BorderThickness" Value="1"/>
            <Setter Property="Background" Value="White"/>
            <Setter Property="Foreground" Value="{StaticResource TextPrimaryBrush}"/>
            <Setter Property="Template">
                <Setter.Value>
                    <ControlTemplate TargetType="TextBox">
                        <Border Background="{TemplateBinding Background}"
                                BorderBrush="{TemplateBinding BorderBrush}"
                                BorderThickness="{TemplateBinding BorderThickness}"
                                CornerRadius="4"
                                SnapsToDevicePixels="True">
                            <ScrollViewer x:Name="PART_ContentHost"
                                          Margin="{TemplateBinding Padding}"
                                          VerticalAlignment="Center"
                                          VerticalScrollBarVisibility="Hidden"
                                          HorizontalScrollBarVisibility="Hidden"/>
                        </Border>
                        <ControlTemplate.Triggers>
                            <Trigger Property="IsFocused" Value="True">
                                <Setter TargetName="PART_ContentHost" Property="Panel.ZIndex" Value="1"/>
                            </Trigger>
                        </ControlTemplate.Triggers>
                    </ControlTemplate>
                </Setter.Value>
            </Setter>
        </Style>

        <Style TargetType="ComboBox">
            <Setter Property="Height" Value="32"/>
            <Setter Property="Padding" Value="8,4"/>
            <Setter Property="FontSize" Value="13"/>
            <Setter Property="Background" Value="White"/>
            <Setter Property="BorderBrush" Value="{StaticResource BorderBrushLight}"/>
        </Style>

        <Style TargetType="CheckBox">
            <Setter Property="VerticalAlignment" Value="Center"/>
            <Setter Property="HorizontalAlignment" Value="Center"/>
        </Style>

        <!-- Primary accent button (rounded, hover/pressed states) -->
        <Style x:Key="PrimaryButton" TargetType="Button">
            <Setter Property="Background" Value="{StaticResource AccentBrush}"/>
            <Setter Property="Foreground" Value="White"/>
            <Setter Property="FontWeight" Value="SemiBold"/>
            <Setter Property="FontSize" Value="13"/>
            <Setter Property="Cursor" Value="Hand"/>
            <Setter Property="BorderThickness" Value="0"/>
            <Setter Property="Template">
                <Setter.Value>
                    <ControlTemplate TargetType="Button">
                        <Border x:Name="Bd" Background="{TemplateBinding Background}" CornerRadius="6">
                            <ContentPresenter HorizontalAlignment="Center" VerticalAlignment="Center"/>
                        </Border>
                        <ControlTemplate.Triggers>
                            <Trigger Property="IsMouseOver" Value="True">
                                <Setter TargetName="Bd" Property="Background" Value="{StaticResource AccentHoverBrush}"/>
                            </Trigger>
                            <Trigger Property="IsPressed" Value="True">
                                <Setter TargetName="Bd" Property="Background" Value="{StaticResource AccentPressedBrush}"/>
                            </Trigger>
                            <Trigger Property="IsEnabled" Value="False">
                                <Setter TargetName="Bd" Property="Background" Value="#93A5C9"/>
                            </Trigger>
                        </ControlTemplate.Triggers>
                    </ControlTemplate>
                </Setter.Value>
            </Setter>
        </Style>

        <!-- Ghost / secondary button -->
        <Style x:Key="GhostButton" TargetType="Button">
            <Setter Property="Background" Value="{StaticResource GhostBrush}"/>
            <Setter Property="Foreground" Value="{StaticResource TextPrimaryBrush}"/>
            <Setter Property="FontSize" Value="12"/>
            <Setter Property="Cursor" Value="Hand"/>
            <Setter Property="BorderThickness" Value="1"/>
            <Setter Property="BorderBrush" Value="{StaticResource BorderBrushLight}"/>
            <Setter Property="Template">
                <Setter.Value>
                    <ControlTemplate TargetType="Button">
                        <Border x:Name="Bd" Background="{TemplateBinding Background}"
                                BorderBrush="{TemplateBinding BorderBrush}" BorderThickness="1" CornerRadius="5">
                            <ContentPresenter HorizontalAlignment="Center" VerticalAlignment="Center" Margin="{TemplateBinding Padding}"/>
                        </Border>
                        <ControlTemplate.Triggers>
                            <Trigger Property="IsMouseOver" Value="True">
                                <Setter TargetName="Bd" Property="Background" Value="{StaticResource GhostHoverBrush}"/>
                            </Trigger>
                            <Trigger Property="IsEnabled" Value="False">
                                <Setter TargetName="Bd" Property="Opacity" Value="0.5"/>
                            </Trigger>
                        </ControlTemplate.Triggers>
                    </ControlTemplate>
                </Setter.Value>
            </Setter>
        </Style>

        <!-- Numbered section badge + title, used as a reusable snippet -->
        <Style x:Key="SectionCard" TargetType="Border">
            <Setter Property="Background" Value="{StaticResource CardBrush}"/>
            <Setter Property="CornerRadius" Value="8"/>
            <Setter Property="Padding" Value="14"/>
            <Setter Property="Margin" Value="0,0,0,12"/>
            <Setter Property="BorderBrush" Value="{StaticResource BorderBrushLight}"/>
            <Setter Property="BorderThickness" Value="1"/>
            <Setter Property="Effect">
                <Setter.Value>
                    <DropShadowEffect Color="#1E293B" Opacity="0.06" BlurRadius="10" ShadowDepth="2" Direction="270"/>
                </Setter.Value>
            </Setter>
        </Style>

    </Window.Resources>

    <Grid Margin="18">
        <Grid.RowDefinitions>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="*"/>
            <RowDefinition Height="Auto"/>
        </Grid.RowDefinitions>

        <!-- Header -->
        <StackPanel Grid.Row="0" Margin="2,0,2,14">
            <TextBlock Text="MEP Route Bank Manager" FontSize="20" FontWeight="Bold" Foreground="{StaticResource TextPrimaryBrush}"/>
            <TextBlock Text="Configure a bank of parallel Cable Tray, Conduit, Duct, or Pipe runs from the selected detail lines." FontSize="12" Foreground="{StaticResource TextSecondaryBrush}" Margin="0,3,0,0"/>
        </StackPanel>

        <!-- Layout Source Panel -->
        <Border Grid.Row="1" Style="{StaticResource SectionCard}">
            <StackPanel>
                <StackPanel Orientation="Horizontal" Margin="0,0,0,10">
                    <Border Background="{StaticResource AccentBrush}" CornerRadius="10" Width="20" Height="20">
                        <TextBlock Text="0" Foreground="White" FontSize="12" FontWeight="Bold" HorizontalAlignment="Center" VerticalAlignment="Center"/>
                    </Border>
                    <TextBlock Text="Layout Source" FontWeight="Bold" FontSize="14" Foreground="{StaticResource TextPrimaryBrush}" Margin="8,0,0,0" VerticalAlignment="Center"/>
                </StackPanel>

                <RadioButton x:Name="rbManual" Content="Manual Grid - I'll set spacing and sizes myself" GroupName="layoutSource" IsChecked="True" FontSize="13" Margin="0,0,0,8"/>
                <RadioButton x:Name="rbMeasured" Content="Match Existing Trays - copy layout from a selection" GroupName="layoutSource" FontSize="13"/>

                <StackPanel x:Name="panelMeasuredOptions" Margin="26,10,0,0" Visibility="Collapsed">
                    <Grid>
                        <Grid.ColumnDefinitions>
                            <ColumnDefinition Width="130"/>
                            <ColumnDefinition Width="*"/>
                            <ColumnDefinition Width="Auto"/>
                        </Grid.ColumnDefinitions>
                        <TextBlock Grid.Column="0" Text="Existing category:" VerticalAlignment="Center" Foreground="{StaticResource TextSecondaryBrush}"/>
                        <ComboBox x:Name="cboRefCategory" Grid.Column="1" Margin="5,0"/>
                        <Button x:Name="btnPickReference" Style="{StaticResource GhostButton}" Grid.Column="2" Content="Pick Existing Elements..." Padding="12,0" Height="32" Margin="8,0,0,0"/>
                    </Grid>
                    <TextBlock x:Name="txtCaptureStatus" FontSize="12" Foreground="{StaticResource TextSecondaryBrush}" Margin="0,8,0,0" TextWrapping="Wrap"/>
                </StackPanel>
            </StackPanel>
        </Border>

        <!-- Global Settings Panel -->
        <Border Grid.Row="2" Style="{StaticResource SectionCard}">
            <StackPanel>
                <StackPanel Orientation="Horizontal" Margin="0,0,0,10">
                    <Border Background="{StaticResource AccentBrush}" CornerRadius="10" Width="20" Height="20">
                        <TextBlock Text="1" Foreground="White" FontSize="12" FontWeight="Bold" HorizontalAlignment="Center" VerticalAlignment="Center"/>
                    </Border>
                    <TextBlock Text="Global Route Parameters" FontWeight="Bold" FontSize="14" Foreground="{StaticResource TextPrimaryBrush}" Margin="8,0,0,0" VerticalAlignment="Center"/>
                </StackPanel>
                <Grid>
                    <Grid.ColumnDefinitions>
                        <ColumnDefinition Width="130"/>
                        <ColumnDefinition Width="*"/>
                        <ColumnDefinition Width="130"/>
                        <ColumnDefinition Width="*"/>
                    </Grid.ColumnDefinitions>
                    <Grid.RowDefinitions>
                        <RowDefinition Height="Auto"/>
                        <RowDefinition Height="Auto"/>
                    </Grid.RowDefinitions>

                    <TextBlock Grid.Row="0" Grid.Column="0" Text="Category:" VerticalAlignment="Center" Foreground="{StaticResource TextSecondaryBrush}" Margin="0,5"/>
                    <ComboBox x:Name="cboCategory" Grid.Row="0" Grid.Column="1" Margin="5"/>

                    <TextBlock Grid.Row="0" Grid.Column="2" Text="Elevation (mm):" VerticalAlignment="Center" Foreground="{StaticResource TextSecondaryBrush}" Margin="10,5,0,5"/>
                    <TextBox x:Name="txtElevation" Grid.Row="0" Grid.Column="3" Text="3000" Margin="5"/>

                    <TextBlock Grid.Row="1" Grid.Column="0" Text="Horiz Gap (mm):" VerticalAlignment="Center" Foreground="{StaticResource TextSecondaryBrush}" Margin="0,5"/>
                    <TextBox x:Name="txtHGap" Grid.Row="1" Grid.Column="1" Text="100" Margin="5"/>

                    <TextBlock Grid.Row="1" Grid.Column="2" Text="Vert Gap (mm):" VerticalAlignment="Center" Foreground="{StaticResource TextSecondaryBrush}" Margin="10,5,0,5"/>
                    <TextBox x:Name="txtVGap" Grid.Row="1" Grid.Column="3" Text="150" Margin="5"/>
                </Grid>
            </StackPanel>
        </Border>

        <!-- Grid Arrangement Panel -->
        <Border Grid.Row="3" Style="{StaticResource SectionCard}">
            <StackPanel>
                <StackPanel Orientation="Horizontal" Margin="0,0,0,10">
                    <Border Background="{StaticResource AccentBrush}" CornerRadius="10" Width="20" Height="20">
                        <TextBlock Text="2" Foreground="White" FontSize="12" FontWeight="Bold" HorizontalAlignment="Center" VerticalAlignment="Center"/>
                    </Border>
                    <TextBlock Text="Grid Layout &amp; Counts" FontWeight="Bold" FontSize="14" Foreground="{StaticResource TextPrimaryBrush}" Margin="8,0,0,0" VerticalAlignment="Center"/>
                </StackPanel>
                <Grid>
                    <Grid.ColumnDefinitions>
                        <ColumnDefinition Width="130"/>
                        <ColumnDefinition Width="*"/>
                        <ColumnDefinition Width="130"/>
                        <ColumnDefinition Width="*"/>
                        <ColumnDefinition Width="Auto"/>
                    </Grid.ColumnDefinitions>

                    <TextBlock Grid.Column="0" Text="Horizontal Count:" VerticalAlignment="Center" Foreground="{StaticResource TextSecondaryBrush}"/>
                    <TextBox x:Name="txtHCount" Grid.Column="1" Text="1" Margin="5"/>

                    <TextBlock Grid.Column="2" Text="Vertical Tiers:" VerticalAlignment="Center" Foreground="{StaticResource TextSecondaryBrush}" Margin="10,0,0,0"/>
                    <TextBox x:Name="txtVCount" Grid.Column="3" Text="1" Margin="5"/>

                    <Button x:Name="btnUpdateGrid" Style="{StaticResource GhostButton}" Grid.Column="4" Content="Update Rows" Padding="12,0" Margin="10,0,0,0" Height="28"/>
                </Grid>
                <StackPanel Orientation="Horizontal" Margin="0,10,0,0">
                    <TextBlock Text="Select which runs to actually model:" VerticalAlignment="Center" Margin="0,0,10,0" Foreground="{StaticResource TextSecondaryBrush}" FontSize="12"/>
                    <Button x:Name="btnSelectAll" Style="{StaticResource GhostButton}" Content="Select All" Padding="10,0" Height="26" Margin="0,0,6,0"/>
                    <Button x:Name="btnSelectNone" Style="{StaticResource GhostButton}" Content="Clear All" Padding="10,0" Height="26"/>
                </StackPanel>
            </StackPanel>
        </Border>

        <!-- Dynamic List Section Title -->
        <StackPanel Grid.Row="4" Orientation="Horizontal" Margin="2,0,2,8">
            <Border Background="{StaticResource AccentBrush}" CornerRadius="10" Width="20" Height="20">
                <TextBlock Text="3" Foreground="White" FontSize="12" FontWeight="Bold" HorizontalAlignment="Center" VerticalAlignment="Center"/>
            </Border>
            <TextBlock Text="Individual Tray / Duct / Pipe Configurations" FontWeight="Bold" FontSize="14" Foreground="{StaticResource TextPrimaryBrush}" Margin="8,0,0,0" VerticalAlignment="Center"/>
            <TextBlock Text="(uncheck to skip a run)" FontSize="12" Foreground="{StaticResource TextSecondaryBrush}" Margin="8,3,0,0" VerticalAlignment="Center"/>
        </StackPanel>

        <!-- Scrollable Dynamic Controls -->
        <Border Grid.Row="5" Style="{StaticResource SectionCard}" Margin="0">
            <ScrollViewer VerticalScrollBarVisibility="Auto">
                <StackPanel x:Name="panelTrayItems"/>
            </ScrollViewer>
        </Border>

        <!-- Action Buttons -->
        <StackPanel Grid.Row="6" Orientation="Horizontal" HorizontalAlignment="Right" Margin="0,14,0,0">
            <Button x:Name="btnCancel" Style="{StaticResource GhostButton}" Content="Cancel" Width="90" Height="34" IsCancel="True" Margin="0,0,10,0"/>
            <Button x:Name="btnOK" Style="{StaticResource PrimaryButton}" Content="Generate Route" Width="160" Height="34" IsDefault="True"/>
        </StackPanel>
    </Grid>
</Window>
"""


class ConfigDialogManager(object):
    def __init__(self, initial_layout=None, preselected_category=None, start_measured=False):
        self.window = XamlReader.Parse(XAML_INTERFACE)
        
        self.rbManual = self.window.FindName("rbManual")
        self.rbMeasured = self.window.FindName("rbMeasured")
        self.panelMeasuredOptions = self.window.FindName("panelMeasuredOptions")
        self.cboRefCategory = self.window.FindName("cboRefCategory")
        self.btnPickReference = self.window.FindName("btnPickReference")
        self.txtCaptureStatus = self.window.FindName("txtCaptureStatus")

        self.cboCategory = self.window.FindName("cboCategory")
        self.txtElevation = self.window.FindName("txtElevation")
        self.txtHGap = self.window.FindName("txtHGap")
        self.txtVGap = self.window.FindName("txtVGap")
        self.txtHCount = self.window.FindName("txtHCount")
        self.txtVCount = self.window.FindName("txtVCount")
        self.btnUpdateGrid = self.window.FindName("btnUpdateGrid")
        self.btnSelectAll = self.window.FindName("btnSelectAll")
        self.btnSelectNone = self.window.FindName("btnSelectNone")
        self.panelTrayItems = self.window.FindName("panelTrayItems")
        self.btnOK = self.window.FindName("btnOK")
        self.btnCancel = self.window.FindName("btnCancel")

        self.row_controls = {}
        self.collected_data = None
        self.requested_pick_category = None

        # When a layout was captured from existing placed elements, the
        # grid shape and per-position offsets are measured, not typed in -
        # so counts/gaps are locked and each row is pre-filled to match.
        self.initial_layout = initial_layout

        for cat in CATEGORY_CONFIG.keys():
            self.cboCategory.Items.Add(cat)
            self.cboRefCategory.Items.Add(cat)

        if preselected_category and preselected_category in CATEGORY_CONFIG:
            self.cboRefCategory.SelectedItem = preselected_category
        else:
            self.cboRefCategory.SelectedIndex = 0

        if preselected_category and preselected_category in CATEGORY_CONFIG:
            self.cboCategory.SelectedItem = preselected_category
        else:
            self.cboCategory.SelectedIndex = 0

        # NOTE: the Checked handlers are wired up further down, so setting
        # IsChecked here does NOT fire on_layout_source_changed - the
        # measured-options panel must be made visible explicitly.
        if self.initial_layout:
            self.rbMeasured.IsChecked = True
            self.panelMeasuredOptions.Visibility = Visibility.Visible
            self._apply_measured_layout_to_fields()
        elif start_measured:
            # Previous pick was cancelled / failed - stay in measured mode
            # so the user can retry without re-selecting the radio button.
            self.rbMeasured.IsChecked = True
            self.panelMeasuredOptions.Visibility = Visibility.Visible
            self.txtCaptureStatus.Text = "Pick existing elements to capture their layout."

        self.rbManual.Checked += self.on_layout_source_changed
        self.rbMeasured.Checked += self.on_layout_source_changed
        self.btnPickReference.Click += self.on_pick_reference_clicked

        self.cboCategory.SelectionChanged += self.on_category_changed
        self.btnUpdateGrid.Click += self.on_update_grid_clicked
        self.btnSelectAll.Click += self.on_select_all_clicked
        self.btnSelectNone.Click += self.on_select_none_clicked
        self.btnOK.Click += self.on_ok_clicked
        self.btnCancel.Click += self.on_cancel_clicked

        self.rebuild_tray_rows()

    def _apply_measured_layout_to_fields(self):
        """Reflect self.initial_layout (already captured) onto the locked
        fields - counts, elevation, gaps - and disable what no longer
        makes sense to hand-edit."""
        self.txtHCount.Text = str(self.initial_layout["count_h"])
        self.txtVCount.Text = str(self.initial_layout["count_v"])
        self.txtElevation.Text = "{:.0f}".format(
            internal_to_mm(self.initial_layout["anchor_elevation_ft"])
        )
        self.txtHGap.IsEnabled = False
        self.txtVGap.IsEnabled = False
        self.txtHGap.Text = "(measured)"
        self.txtVGap.Text = "(measured)"
        self.txtHCount.IsEnabled = False
        self.txtVCount.IsEnabled = False
        self.btnUpdateGrid.IsEnabled = False
        self.cboCategory.IsEnabled = False

        status = "Captured {0} existing element(s): {1} tier(s) x up to {2} run(s) per tier.".format(
            self.initial_layout["source_count"],
            self.initial_layout["count_v"],
            self.initial_layout["count_h"],
        )
        if self.initial_layout["ragged"]:
            status += " Some tiers had fewer runs - those slots are unticked below; review before generating."
        self.txtCaptureStatus.Text = status

    def _clear_measured_layout(self):
        self.initial_layout = None
        self.txtCaptureStatus.Text = ""
        self.txtHGap.IsEnabled = True
        self.txtVGap.IsEnabled = True
        if self.txtHGap.Text == "(measured)":
            self.txtHGap.Text = "100"
        if self.txtVGap.Text == "(measured)":
            self.txtVGap.Text = "150"
        self.txtHCount.IsEnabled = True
        self.txtVCount.IsEnabled = True
        self.btnUpdateGrid.IsEnabled = True
        self.cboCategory.IsEnabled = True

    def on_layout_source_changed(self, sender, e):
        if self.rbMeasured.IsChecked:
            self.panelMeasuredOptions.Visibility = Visibility.Visible
            if not self.initial_layout:
                self.txtCaptureStatus.Text = "Pick existing elements to capture their layout."
        else:
            self.panelMeasuredOptions.Visibility = Visibility.Collapsed
            if self.initial_layout:
                self._clear_measured_layout()
                self.rebuild_tray_rows()

    def on_pick_reference_clicked(self, sender, e):
        # Picking elements in the Revit view requires Revit's main window
        # to be genuinely interactive. Hiding this window mid-ShowDialog()
        # and Show()-ing it again leaves WPF's modal bookkeeping out of
        # sync, which can leave Revit's window stuck disabled afterwards.
        # So instead: close this dialog cleanly, let main() do the pick
        # with no WPF window open at all, then reopen a fresh dialog
        # pre-filled with the captured layout.
        self.requested_pick_category = str(self.cboRefCategory.SelectedItem)
        self.window.DialogResult = False
        self.window.Close()

    def get_loaded_types(self, category_label):
        cfg = CATEGORY_CONFIG[category_label]
        types = (
            DB.FilteredElementCollector(doc)
            .OfClass(cfg["type_class"])
            .WhereElementIsElementType()
            .ToElements()
        )
        return OrderedDict((type_name(tt), tt) for tt in types)

    def get_loaded_system_types(self, category_label):
        cfg = CATEGORY_CONFIG[category_label]
        if not cfg["needs_system_type"]:
            return {}
        sys_types = (
            DB.FilteredElementCollector(doc)
            .OfClass(cfg["system_type_class"])
            .OfCategory(cfg["system_type_category"])
            .ToElements()
        )
        return OrderedDict((safe_name(st), st) for st in sys_types)

    def rebuild_tray_rows(self):
        # Preserve prior include/exclude choices (by grid position) across
        # category switches or grid-count updates, so re-configuring the
        # grid doesn't silently reset which runs the user picked to model.
        prior_included = dict(
            (pos, ctrl["include_chk"].IsChecked)
            for pos, ctrl in self.row_controls.items()
        )

        self.panelTrayItems.Children.Clear()
        self.row_controls.clear()

        category_label = str(self.cboCategory.SelectedItem)
        cfg = CATEGORY_CONFIG[category_label]

        try:
            count_h = int(self.txtHCount.Text)
            count_v = int(self.txtVCount.Text)
            if count_h < 1 or count_v < 1:
                raise ValueError()
        except Exception:
            MessageBox.Show("Horizontal and Vertical counts must be integers >= 1.", "Input Error")
            return

        types_dict = self.get_loaded_types(category_label)
        if not types_dict:
            MessageBox.Show("No loaded types found for " + category_label, "Error")
            return

        sys_types_dict = self.get_loaded_system_types(category_label)

        measured_grid = self.initial_layout["grid"] if self.initial_layout else {}

        for row in range(count_v):
            tier_accent = hex_brush(TIER_ACCENT_PALETTE[row % len(TIER_ACCENT_PALETTE)])
            for col in range(count_h):
                border = Border()
                border.Background = hex_brush("#F8FAFC") if row % 2 == 0 else Brushes.White
                border.BorderBrush = hex_brush("#E2E8F0")
                border.BorderThickness = Thickness(1, 1, 1, 1)
                border.CornerRadius = CornerRadius(6)
                border.Margin = Thickness(0, 0, 0, 8)
                border.Padding = Thickness(0)

                # Left accent stripe groups rows visually by tier
                accent_strip = Border()
                accent_strip.Background = tier_accent
                accent_strip.CornerRadius = CornerRadius(6, 0, 0, 6)

                row_container = Grid()
                row_container.ColumnDefinitions.Add(ColumnDefinition())  # accent stripe
                row_container.ColumnDefinitions.Add(ColumnDefinition())  # fields
                row_container.ColumnDefinitions[0].Width = GridLength(4)
                Grid.SetColumn(accent_strip, 0)
                row_container.Children.Add(accent_strip)

                grid = Grid()
                Grid.SetColumn(grid, 1)
                grid.Margin = Thickness(10, 8, 10, 8)
                grid.ColumnDefinitions.Add(ColumnDefinition())  # Include checkbox
                grid.ColumnDefinitions.Add(ColumnDefinition())  # Label
                grid.ColumnDefinitions.Add(ColumnDefinition())  # Type
                if cfg["needs_system_type"]:
                    grid.ColumnDefinitions.Add(ColumnDefinition())  # System Type
                grid.ColumnDefinitions.Add(ColumnDefinition())  # Width / Dia
                if cfg["size_mode"] != "round":
                    grid.ColumnDefinitions.Add(ColumnDefinition())  # Height

                grid.ColumnDefinitions[0].Width = GridLength(30)
                grid.ColumnDefinitions[1].Width = GridLength(110)

                # Include checkbox - lets the user pick exactly which grid
                # positions should actually be modeled, without having to
                # shrink the H x V grid itself.
                measured_cell = measured_grid.get((row, col))
                default_included = measured_cell["included"] if measured_cell else True
                chk_include = CheckBox()
                chk_include.IsChecked = prior_included.get((row, col), default_included)
                chk_include.VerticalAlignment = VerticalAlignment.Center
                chk_include.HorizontalAlignment = HorizontalAlignment.Center
                chk_include.ToolTip = "Untick to skip this run - it will not be generated."
                Grid.SetColumn(chk_include, 0)
                grid.Children.Add(chk_include)

                # Label
                lbl = TextBlock()
                lbl_text = "Tier {}, Run {}:".format(row + 1, col + 1)
                if measured_cell is not None and not measured_cell["included"]:
                    lbl_text += " (no match)"
                lbl.Text = lbl_text
                lbl.FontWeight = FontWeights.SemiBold
                lbl.Foreground = hex_brush("#111827")
                lbl.VerticalAlignment = VerticalAlignment.Center
                Grid.SetColumn(lbl, 1)
                grid.Children.Add(lbl)

                # Type Combo
                cbo_type = ComboBox()
                cbo_type.Margin = Thickness(4)
                cbo_type.Height = 24
                for name in types_dict.keys():
                    cbo_type.Items.Add(name)
                if measured_cell is not None:
                    matched_name = type_name(measured_cell["type"])
                    if matched_name in types_dict:
                        cbo_type.SelectedItem = matched_name
                    else:
                        cbo_type.SelectedIndex = 0
                else:
                    cbo_type.SelectedIndex = 0
                Grid.SetColumn(cbo_type, 2)
                grid.Children.Add(cbo_type)

                col_idx = 3

                # System Type Combo if needed
                cbo_sys_type = None
                if cfg["needs_system_type"]:
                    cbo_sys_type = ComboBox()
                    cbo_sys_type.Margin = Thickness(4)
                    cbo_sys_type.Height = 24
                    for name in sys_types_dict.keys():
                        cbo_sys_type.Items.Add(name)
                    cbo_sys_type.SelectedIndex = 0
                    Grid.SetColumn(cbo_sys_type, col_idx)
                    grid.Children.Add(cbo_sys_type)
                    col_idx += 1

                # Width / Dia
                default_w = "300" if cfg["size_mode"] != "round" else "150"
                default_h = "150"
                if measured_cell is not None:
                    size_mm = measured_cell["size_mm"]
                    if "diameter_mm" in size_mm:
                        default_w = "{:.0f}".format(size_mm["diameter_mm"])
                    elif "width_mm" in size_mm:
                        default_w = "{:.0f}".format(size_mm["width_mm"])
                        default_h = "{:.0f}".format(size_mm.get("height_mm", 150))

                txt_w = TextBox()
                txt_w.Text = default_w
                txt_w.Margin = Thickness(4)
                txt_w.Height = 24
                txt_w.VerticalContentAlignment = VerticalAlignment.Center
                Grid.SetColumn(txt_w, col_idx)
                grid.Children.Add(txt_w)
                col_idx += 1

                # Height
                txt_h = None
                if cfg["size_mode"] != "round":
                    txt_h = TextBox()
                    txt_h.Text = default_h
                    txt_h.Margin = Thickness(4)
                    txt_h.Height = 24
                    txt_h.VerticalContentAlignment = VerticalAlignment.Center
                    Grid.SetColumn(txt_h, col_idx)
                    grid.Children.Add(txt_h)

                row_container.Children.Add(grid)
                border.Child = row_container
                self.panelTrayItems.Children.Add(border)

                row_ctrl = {
                    "include_chk": chk_include,
                    "type_combo": cbo_type,
                    "types_dict": types_dict,
                    "sys_type_combo": cbo_sys_type,
                    "sys_types_dict": sys_types_dict,
                    "txt_w": txt_w,
                    "txt_h": txt_h,
                    "size_mode": cfg["size_mode"],
                }
                self.row_controls[(row, col)] = row_ctrl

                other_controls = [cbo_type, txt_w]
                if cbo_sys_type is not None:
                    other_controls.append(cbo_sys_type)
                if txt_h is not None:
                    other_controls.append(txt_h)

                def make_toggle_handler(border=border, controls=other_controls):
                    def _toggle(sender, e):
                        is_on = bool(sender.IsChecked)
                        border.Opacity = 1.0 if is_on else 0.45
                        for ctrl in controls:
                            ctrl.IsEnabled = is_on
                    return _toggle

                toggle_handler = make_toggle_handler()
                chk_include.Checked += toggle_handler
                chk_include.Unchecked += toggle_handler
                toggle_handler(chk_include, None)

    def on_category_changed(self, sender, e):
        self.rebuild_tray_rows()

    def on_update_grid_clicked(self, sender, e):
        self.rebuild_tray_rows()

    def on_select_all_clicked(self, sender, e):
        for ctrl in self.row_controls.values():
            ctrl["include_chk"].IsChecked = True

    def on_select_none_clicked(self, sender, e):
        for ctrl in self.row_controls.values():
            ctrl["include_chk"].IsChecked = False

    def on_ok_clicked(self, sender, e):
        try:
            category_label = str(self.cboCategory.SelectedItem)
            elevation_mm = float(self.txtElevation.Text)

            if self.initial_layout:
                # Gaps aren't used in measured mode - offsets come straight
                # from the elements that were selected as the reference.
                h_gap_mm = 0.0
                v_gap_mm = 0.0
            else:
                h_gap_mm = float(self.txtHGap.Text)
                v_gap_mm = float(self.txtVGap.Text)

            count_h = int(self.txtHCount.Text)
            count_v = int(self.txtVCount.Text)

            if count_h < 1 or count_v < 1:
                raise ValueError("Grid counts must be >= 1")

            # The counts may have been edited without pressing "Update Rows".
            # main() indexes grid_config by every (row, col) in the counts,
            # so a mismatch would crash with a KeyError - refresh instead.
            expected_positions = set(
                (r, c) for r in range(count_v) for c in range(count_h)
            )
            if set(self.row_controls.keys()) != expected_positions:
                self.rebuild_tray_rows()
                MessageBox.Show(
                    "The grid counts changed, so the run rows were refreshed.\n"
                    "Review them and click Generate Route again.",
                    "Rows Updated",
                )
                return

            grid_config = {}
            for (row, col), ctrl in self.row_controls.items():
                if ctrl["type_combo"].SelectedItem is None:
                    raise ValueError("Tier {0}, Run {1}: no type selected.".format(row + 1, col + 1))
                selected_type_name = str(ctrl["type_combo"].SelectedItem)
                chosen_type = ctrl["types_dict"][selected_type_name]

                chosen_sys_type = None
                if ctrl["sys_type_combo"]:
                    if ctrl["sys_type_combo"].SelectedItem is None:
                        raise ValueError(
                            "No system type is loaded/selected for {0} - "
                            "add a system type to the project first.".format(category_label)
                        )
                    selected_sys_name = str(ctrl["sys_type_combo"].SelectedItem)
                    chosen_sys_type = ctrl["sys_types_dict"][selected_sys_name]

                if ctrl["size_mode"] == "round":
                    diameter_mm = float(ctrl["txt_w"].Text)
                    size_values = {"diameter_mm": diameter_mm}
                else:
                    width_mm = float(ctrl["txt_w"].Text)
                    height_mm = float(ctrl["txt_h"].Text)
                    size_values = {"width_mm": width_mm, "height_mm": height_mm}

                grid_config[(row, col)] = {
                    "type": chosen_type,
                    "sys_type": chosen_sys_type,
                    "size": size_values,
                    "included": bool(ctrl["include_chk"].IsChecked),
                }

            if not any(item["included"] for item in grid_config.values()):
                raise ValueError("At least one run must be checked as included.")

            self.collected_data = {
                "category_label": category_label,
                "elevation_mm": elevation_mm,
                "h_gap_mm": h_gap_mm,
                "v_gap_mm": v_gap_mm,
                "count_h": count_h,
                "count_v": count_v,
                "grid_config": grid_config,
                "layout_mode": "measured" if self.initial_layout else "manual",
                "measured_offsets": self.initial_layout["grid"] if self.initial_layout else None,
            }

            self.window.DialogResult = True
            self.window.Close()

        except Exception as ex:
            MessageBox.Show("Please check all numerical inputs: " + str(ex), "Validation Error")

    def on_cancel_clicked(self, sender, e):
        self.window.DialogResult = False
        self.window.Close()

    def show_dialog(self):
        result = self.window.ShowDialog()
        return self.collected_data if result else None


# --------------------------------------------------------------------------
# Result Summary Popup - shown after generation instead of a plain alert.
# The full clickable-element report still goes to the pyRevit output panel;
# this window is a quick, scannable at-a-glance result.
# --------------------------------------------------------------------------
XAML_SUMMARY_WINDOW = """
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="Route Generation Summary" Height="580" Width="480"
        WindowStartupLocation="CenterScreen" Background="#EEF1F5"
        FontFamily="Segoe UI" ResizeMode="NoResize">
    <Window.Resources>
        <SolidColorBrush x:Key="TextPrimaryBrush" Color="#111827"/>
        <SolidColorBrush x:Key="TextSecondaryBrush" Color="#6B7280"/>
        <SolidColorBrush x:Key="BorderBrushLight" Color="#E2E8F0"/>
        <SolidColorBrush x:Key="AccentBrush" Color="#2563EB"/>
        <SolidColorBrush x:Key="AccentHoverBrush" Color="#1D4ED8"/>
        <SolidColorBrush x:Key="AccentPressedBrush" Color="#1E40AF"/>
        <SolidColorBrush x:Key="GhostBrush" Color="#F1F5F9"/>
        <SolidColorBrush x:Key="GhostHoverBrush" Color="#E2E8F0"/>

        <Style x:Key="PrimaryButton" TargetType="Button">
            <Setter Property="Background" Value="{StaticResource AccentBrush}"/>
            <Setter Property="Foreground" Value="White"/>
            <Setter Property="FontWeight" Value="SemiBold"/>
            <Setter Property="FontSize" Value="13"/>
            <Setter Property="Cursor" Value="Hand"/>
            <Setter Property="BorderThickness" Value="0"/>
            <Setter Property="Template">
                <Setter.Value>
                    <ControlTemplate TargetType="Button">
                        <Border x:Name="Bd" Background="{TemplateBinding Background}" CornerRadius="6">
                            <ContentPresenter HorizontalAlignment="Center" VerticalAlignment="Center"/>
                        </Border>
                        <ControlTemplate.Triggers>
                            <Trigger Property="IsMouseOver" Value="True">
                                <Setter TargetName="Bd" Property="Background" Value="{StaticResource AccentHoverBrush}"/>
                            </Trigger>
                            <Trigger Property="IsPressed" Value="True">
                                <Setter TargetName="Bd" Property="Background" Value="{StaticResource AccentPressedBrush}"/>
                            </Trigger>
                        </ControlTemplate.Triggers>
                    </ControlTemplate>
                </Setter.Value>
            </Setter>
        </Style>

        <Style x:Key="GhostButton" TargetType="Button">
            <Setter Property="Background" Value="{StaticResource GhostBrush}"/>
            <Setter Property="Foreground" Value="{StaticResource TextPrimaryBrush}"/>
            <Setter Property="FontSize" Value="12"/>
            <Setter Property="Cursor" Value="Hand"/>
            <Setter Property="BorderThickness" Value="1"/>
            <Setter Property="BorderBrush" Value="{StaticResource BorderBrushLight}"/>
            <Setter Property="Template">
                <Setter.Value>
                    <ControlTemplate TargetType="Button">
                        <Border x:Name="Bd" Background="{TemplateBinding Background}"
                                BorderBrush="{TemplateBinding BorderBrush}" BorderThickness="1" CornerRadius="5">
                            <ContentPresenter HorizontalAlignment="Center" VerticalAlignment="Center" Margin="{TemplateBinding Padding}"/>
                        </Border>
                        <ControlTemplate.Triggers>
                            <Trigger Property="IsMouseOver" Value="True">
                                <Setter TargetName="Bd" Property="Background" Value="{StaticResource GhostHoverBrush}"/>
                            </Trigger>
                        </ControlTemplate.Triggers>
                    </ControlTemplate>
                </Setter.Value>
            </Setter>
        </Style>
    </Window.Resources>

    <Grid Margin="20">
        <Grid.RowDefinitions>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="*"/>
            <RowDefinition Height="Auto"/>
        </Grid.RowDefinitions>

        <!-- Status banner -->
        <Border x:Name="bannerBorder" Grid.Row="0" CornerRadius="8" Padding="14" Margin="0,0,0,14">
            <StackPanel Orientation="Horizontal">
                <TextBlock x:Name="txtBannerIcon" FontSize="24" VerticalAlignment="Center" Margin="0,0,12,0"/>
                <StackPanel VerticalAlignment="Center">
                    <TextBlock x:Name="txtBannerTitle" FontWeight="Bold" FontSize="15"/>
                    <TextBlock x:Name="txtBannerSubtitle" FontSize="12" Margin="0,2,0,0"/>
                </StackPanel>
            </StackPanel>
        </Border>

        <TextBlock Grid.Row="1" x:Name="txtMeta" FontSize="12" Foreground="{StaticResource TextSecondaryBrush}" Margin="0,0,0,14" TextWrapping="Wrap"/>

        <!-- Stat cards -->
        <UniformGrid Grid.Row="2" Rows="1" Columns="4" Margin="0,0,0,14">
            <Border Background="White" CornerRadius="6" Margin="0,0,6,0" Padding="6,10" BorderBrush="{StaticResource BorderBrushLight}" BorderThickness="1">
                <StackPanel>
                    <TextBlock x:Name="txtStatRuns" FontSize="19" FontWeight="Bold" Foreground="{StaticResource AccentBrush}" HorizontalAlignment="Center"/>
                    <TextBlock Text="Runs" FontSize="10" Foreground="{StaticResource TextSecondaryBrush}" HorizontalAlignment="Center"/>
                </StackPanel>
            </Border>
            <Border Background="White" CornerRadius="6" Margin="0,0,6,0" Padding="6,10" BorderBrush="{StaticResource BorderBrushLight}" BorderThickness="1">
                <StackPanel>
                    <TextBlock x:Name="txtStatSegments" FontSize="19" FontWeight="Bold" Foreground="{StaticResource AccentBrush}" HorizontalAlignment="Center"/>
                    <TextBlock Text="Segments" FontSize="10" Foreground="{StaticResource TextSecondaryBrush}" HorizontalAlignment="Center"/>
                </StackPanel>
            </Border>
            <Border Background="White" CornerRadius="6" Margin="0,0,6,0" Padding="6,10" BorderBrush="{StaticResource BorderBrushLight}" BorderThickness="1">
                <StackPanel>
                    <TextBlock x:Name="txtStatLength" FontSize="14" FontWeight="Bold" Foreground="{StaticResource AccentBrush}" HorizontalAlignment="Center"/>
                    <TextBlock Text="Length" FontSize="10" Foreground="{StaticResource TextSecondaryBrush}" HorizontalAlignment="Center"/>
                </StackPanel>
            </Border>
            <Border Background="White" CornerRadius="6" Padding="6,10" BorderBrush="{StaticResource BorderBrushLight}" BorderThickness="1">
                <StackPanel>
                    <TextBlock x:Name="txtStatFittings" FontSize="19" FontWeight="Bold" Foreground="{StaticResource AccentBrush}" HorizontalAlignment="Center"/>
                    <TextBlock Text="Fittings" FontSize="10" Foreground="{StaticResource TextSecondaryBrush}" HorizontalAlignment="Center"/>
                </StackPanel>
            </Border>
        </UniformGrid>

        <!-- Issues / all-clear list -->
        <Border Grid.Row="3" Background="White" CornerRadius="6" BorderBrush="{StaticResource BorderBrushLight}" BorderThickness="1" Padding="12">
            <DockPanel>
                <TextBlock x:Name="txtIssuesTitle" DockPanel.Dock="Top" FontWeight="Bold" FontSize="12" Foreground="{StaticResource TextPrimaryBrush}" Margin="0,0,0,8"/>
                <ScrollViewer VerticalScrollBarVisibility="Auto">
                    <StackPanel x:Name="panelIssues"/>
                </ScrollViewer>
            </DockPanel>
        </Border>

        <!-- Buttons -->
        <StackPanel Grid.Row="4" Orientation="Horizontal" HorizontalAlignment="Right" Margin="0,14,0,0">
            <Button x:Name="btnViewReport" Style="{StaticResource GhostButton}" Content="View Full Report" Padding="12,0" Height="32" Margin="0,0,10,0"/>
            <Button x:Name="btnClose" Style="{StaticResource PrimaryButton}" Content="Close" Width="100" Height="32"/>
        </StackPanel>
    </Grid>
</Window>
"""


class SummaryDialog(object):
    def __init__(self, data):
        self.window = XamlReader.Parse(XAML_SUMMARY_WINDOW)

        banner = self.window.FindName("bannerBorder")
        icon = self.window.FindName("txtBannerIcon")
        title = self.window.FindName("txtBannerTitle")
        subtitle = self.window.FindName("txtBannerSubtitle")
        meta = self.window.FindName("txtMeta")
        stat_runs = self.window.FindName("txtStatRuns")
        stat_segments = self.window.FindName("txtStatSegments")
        stat_length = self.window.FindName("txtStatLength")
        stat_fittings = self.window.FindName("txtStatFittings")
        issues_title = self.window.FindName("txtIssuesTitle")
        panel_issues = self.window.FindName("panelIssues")
        btn_view_report = self.window.FindName("btnViewReport")
        btn_close = self.window.FindName("btnClose")

        has_issues = bool(data["issue_lines"])

        if has_issues:
            banner.Background = hex_brush("#FEF3C7")
            icon.Text = "\u26A0"
            icon.Foreground = hex_brush("#D97706")
            title.Text = "Generated with items to review"
            title.Foreground = hex_brush("#92400E")
            subtitle.Text = "{0} item(s) need a look".format(len(data["issue_lines"]))
            subtitle.Foreground = hex_brush("#92400E")
        else:
            banner.Background = hex_brush("#DCFCE7")
            icon.Text = "\u2713"
            icon.Foreground = hex_brush("#16A34A")
            title.Text = "Route generated successfully"
            title.Foreground = hex_brush("#166534")
            subtitle.Text = "All segments and fittings placed cleanly"
            subtitle.Foreground = hex_brush("#166534")

        meta.Text = "{0}  |  Layout source: {1}  |  {2}".format(
            data["category_label"], data["layout_source_label"], data["arrangement_text"]
        )

        stat_runs.Text = str(data["included_count"])
        stat_segments.Text = str(data["segment_count"])
        stat_length.Text = data["length_text"]
        stat_fittings.Text = str(data["fitting_count"])

        if has_issues:
            issues_title.Text = "Needs review"
            for line in data["issue_lines"]:
                tb = TextBlock()
                tb.Text = u"\u2022 " + line
                tb.TextWrapping = TextWrapping.Wrap
                tb.FontSize = 12
                tb.Margin = Thickness(0, 0, 0, 6)
                tb.Foreground = hex_brush("#92400E")
                panel_issues.Children.Add(tb)
        else:
            issues_title.Text = "No issues detected"
            tb = TextBlock()
            tb.Text = "Every straight segment and fitting was created without errors."
            tb.TextWrapping = TextWrapping.Wrap
            tb.FontSize = 12
            tb.Foreground = hex_brush("#6B7280")
            panel_issues.Children.Add(tb)

        btn_view_report.Click += self.on_view_report
        btn_close.Click += self.on_close

    def on_view_report(self, sender, e):
        try:
            output.show()
        except Exception:
            pass

    def on_close(self, sender, e):
        self.window.Close()

    def show(self):
        self.window.ShowDialog()


# --------------------------------------------------------------------------
# Selection & Topological Graph Helpers
# --------------------------------------------------------------------------
class DetailLineSelectionFilter(UI.Selection.ISelectionFilter):
    def AllowElement(self, element):
        return (
            isinstance(element, DB.CurveElement)
            and element.CurveElementType == DB.CurveElementType.DetailCurve
        )

    def AllowReference(self, reference, position):
        return True


def get_detail_lines(category_label):
    pre_selected = [doc.GetElement(eid) for eid in uidoc.Selection.GetElementIds()]
    detail_lines = [
        el for el in pre_selected
        if isinstance(el, DB.CurveElement) and el.CurveElementType == DB.CurveElementType.DetailCurve
    ]
    if not detail_lines:
        try:
            refs = uidoc.Selection.PickObjects(
                UI.Selection.ObjectType.Element,
                DetailLineSelectionFilter(),
                "Select detail lines for the {0} route".format(category_label),
            )
        except Exception:
            script.exit()
        detail_lines = [doc.GetElement(r.ElementId) for r in refs]

    owner_view_ids = set(el.OwnerViewId for el in detail_lines)
    if len(owner_view_ids) > 1:
        forms.alert("Selected detail lines must be from a single view only.", exitscript=True)

    return detail_lines


# --------------------------------------------------------------------------
# Reference Layout Capture - derive a run bank layout from already-placed
# elements instead of manual grid/gap configuration.
# --------------------------------------------------------------------------
TIER_CLUSTER_TOL_FT = mm_to_internal(20.0)  # elements within this Z band count as the same tier


def capture_reference_layout(category_label):
    """Let the user select existing placed elements of category_label
    (e.g. real Cable Tray runs already modeled elsewhere) and derive a
    grid layout - type, size, and relative horizontal/vertical offsets -
    that can be replicated onto a new detail-line path.

    Returns (layout_dict, error_message). layout_dict is None on failure/
    cancel, with error_message explaining why.
    """
    cfg = CATEGORY_CONFIG[category_label]
    instance_class = cfg["instance_class"]

    class ReferenceElementFilter(UI.Selection.ISelectionFilter):
        def AllowElement(self, element):
            return isinstance(element, instance_class)

        def AllowReference(self, reference, position):
            return True

    pre_selected = [doc.GetElement(eid) for eid in uidoc.Selection.GetElementIds()]
    ref_elements = [el for el in pre_selected if isinstance(el, instance_class)]

    if not ref_elements:
        try:
            refs = uidoc.Selection.PickObjects(
                UI.Selection.ObjectType.Element,
                ReferenceElementFilter(),
                "Select existing {0} elements to copy the layout from".format(category_label),
            )
        except Exception:
            return None, "Selection cancelled."
        ref_elements = [doc.GetElement(r.ElementId) for r in refs]

    if not ref_elements:
        return None, "No existing {0} elements were selected.".format(category_label)

    infos = []
    for el in ref_elements:
        location = getattr(el, "Location", None)
        curve = getattr(location, "Curve", None) if location else None
        if curve is None:
            continue

        p1 = curve.GetEndPoint(0)
        p2 = curve.GetEndPoint(1)
        dx, dy = p2.X - p1.X, p2.Y - p1.Y
        length = math.sqrt(dx * dx + dy * dy)
        normal = DB.XYZ(-dy / length, dx / length, 0) if length > 1e-6 else DB.XYZ(0, 1, 0)
        mid = DB.XYZ((p1.X + p2.X) / 2.0, (p1.Y + p2.Y) / 2.0, (p1.Z + p2.Z) / 2.0)

        try:
            elem_type = doc.GetElement(el.GetTypeId())
        except Exception:
            elem_type = None
        if elem_type is None:
            continue

        size_mm = read_size_from_element(cfg, el)
        if not size_mm:
            continue

        infos.append({"element": el, "type": elem_type, "size_mm": size_mm, "mid": mid, "normal": normal})

    if not infos:
        return None, (
            "None of the selected elements had a readable straight location and size - "
            "check they are not arcs/splines and have valid width/height or diameter."
        )

    # Assume the selected runs are roughly parallel; use the first
    # element's perpendicular direction as the shared axis for measuring
    # horizontal offsets, and use the lowest / left-most element as the
    # anchor (offset 0, 0) that every other offset is measured against.
    ref_normal = infos[0]["normal"]
    anchor = min(
        infos,
        key=lambda info: (
            round(info["mid"].Z, 6),
            info["mid"].X * ref_normal.X + info["mid"].Y * ref_normal.Y,
        ),
    )
    anchor_mid = anchor["mid"]

    for info in infos:
        info["h_offset_ft"] = (
            (info["mid"].X - anchor_mid.X) * ref_normal.X
            + (info["mid"].Y - anchor_mid.Y) * ref_normal.Y
        )
        info["v_offset_ft"] = info["mid"].Z - anchor_mid.Z

    # Cluster into elevation tiers, then order each tier left-to-right.
    tiers = []
    for info in sorted(infos, key=lambda i: i["v_offset_ft"]):
        for tier in tiers:
            if abs(tier[0]["v_offset_ft"] - info["v_offset_ft"]) < TIER_CLUSTER_TOL_FT:
                tier.append(info)
                break
        else:
            tiers.append([info])

    for tier in tiers:
        tier.sort(key=lambda i: i["h_offset_ft"])

    count_v = len(tiers)
    count_h = max(len(tier) for tier in tiers)

    measured_grid = {}
    ragged = False
    for row, tier in enumerate(tiers):
        tier_v_offset_ft = sum(i["v_offset_ft"] for i in tier) / float(len(tier))
        for col in range(count_h):
            if col < len(tier):
                info = tier[col]
                measured_grid[(row, col)] = {
                    "type": info["type"],
                    "size_mm": info["size_mm"],
                    "h_offset_ft": info["h_offset_ft"],
                    "v_offset_ft": tier_v_offset_ft,
                    "included": True,
                }
            else:
                ragged = True
                fallback = tier[-1]
                measured_grid[(row, col)] = {
                    "type": fallback["type"],
                    "size_mm": fallback["size_mm"],
                    "h_offset_ft": fallback["h_offset_ft"],
                    "v_offset_ft": tier_v_offset_ft,
                    "included": False,
                }

    layout = {
        "count_h": count_h,
        "count_v": count_v,
        "grid": measured_grid,
        "source_count": len(infos),
        "ragged": ragged,
        "anchor_elevation_ft": anchor_mid.Z,
    }
    return layout, None


def node_key(point):
    return (
        round(point.X / NODE_TOLERANCE_FT),
        round(point.Y / NODE_TOLERANCE_FT),
        round(point.Z / NODE_TOLERANCE_FT),
    )


class RouteNode:
    def __init__(self, key, point):
        self.key = key
        self.point = point
        self.edges = []


class RouteEdge:
    def __init__(self, edge_id, element, curve, node1_key, node2_key):
        self.id = edge_id
        self.element = element
        self.curve = curve
        self.node1_key = node1_key
        self.node2_key = node2_key
        self.start_node_key = None
        self.end_node_key = None
        self.oriented_dir = None
        self.oriented_normal = None


def build_and_orient_graph(segments):
    nodes = {}
    edges = []

    for idx, (el, curve) in enumerate(segments):
        p1 = curve.GetEndPoint(0)
        p2 = curve.GetEndPoint(1)
        k1 = node_key(p1)
        k2 = node_key(p2)

        if k1 not in nodes:
            nodes[k1] = RouteNode(k1, p1)
        if k2 not in nodes:
            nodes[k2] = RouteNode(k2, p2)

        edge = RouteEdge(idx, el, curve, k1, k2)
        edges.append(edge)
        nodes[k1].edges.append(edge)
        nodes[k2].edges.append(edge)

    visited_edges = set()

    for start_edge in edges:
        if start_edge.id in visited_edges:
            continue

        start_edge.start_node_key = start_edge.node1_key
        start_edge.end_node_key = start_edge.node2_key
        visited_edges.add(start_edge.id)
        queue = [start_edge]

        while queue:
            curr = queue.pop(0)
            p_start = nodes[curr.start_node_key].point
            p_end = nodes[curr.end_node_key].point
            dx = p_end.X - p_start.X
            dy = p_end.Y - p_start.Y
            length = math.sqrt(dx * dx + dy * dy)

            if length < 1e-6:
                curr.oriented_dir = DB.XYZ(1, 0, 0)
                curr.oriented_normal = DB.XYZ(0, 1, 0)
            else:
                ux, uy = dx / length, dy / length
                curr.oriented_dir = DB.XYZ(ux, uy, 0)
                curr.oriented_normal = DB.XYZ(-uy, ux, 0)

            end_node = nodes[curr.end_node_key]
            for next_edge in end_node.edges:
                if next_edge.id not in visited_edges:
                    visited_edges.add(next_edge.id)
                    if next_edge.node1_key == curr.end_node_key:
                        next_edge.start_node_key = next_edge.node1_key
                        next_edge.end_node_key = next_edge.node2_key
                    else:
                        next_edge.start_node_key = next_edge.node2_key
                        next_edge.end_node_key = next_edge.node1_key
                    queue.append(next_edge)

            start_node = nodes[curr.start_node_key]
            for prev_edge in start_node.edges:
                if prev_edge.id not in visited_edges:
                    visited_edges.add(prev_edge.id)
                    if prev_edge.node2_key == curr.start_node_key:
                        prev_edge.start_node_key = prev_edge.node1_key
                        prev_edge.end_node_key = prev_edge.node2_key
                    else:
                        prev_edge.start_node_key = prev_edge.node2_key
                        prev_edge.end_node_key = prev_edge.node1_key
                    queue.append(prev_edge)

    node_miter_vecs = {}
    for k, node in nodes.items():
        if len(node.edges) == 1:
            node_miter_vecs[k] = node.edges[0].oriented_normal
        elif len(node.edges) == 2:
            e1, e2 = node.edges[0], node.edges[1]
            n1, n2 = e1.oriented_normal, e2.oriented_normal
            dot = n1.X * n2.X + n1.Y * n2.Y
            denom = 1.0 + dot
            if denom > 0.01:
                mx = (n1.X + n2.X) / denom
                my = (n1.Y + n2.Y) / denom
                node_miter_vecs[k] = DB.XYZ(mx, my, 0)
            else:
                node_miter_vecs[k] = n1
        else:
            sum_x = sum(e.oriented_normal.X for e in node.edges)
            sum_y = sum(e.oriented_normal.Y for e in node.edges)
            count = float(len(node.edges))
            node_miter_vecs[k] = DB.XYZ(sum_x / count, sum_y / count, 0)

    return nodes, edges, node_miter_vecs


# --------------------------------------------------------------------------
# Fitting Placement Helpers
# --------------------------------------------------------------------------
def is_parallel(dir_a, dir_b, tol_deg=PARALLEL_TOLERANCE_DEG):
    angle_deg = math.degrees(dir_a.Normalize().AngleTo(dir_b.Normalize()))
    return angle_deg < tol_deg or abs(angle_deg - 180.0) < tol_deg


def pick_level_for_elevation(elevation_ft):
    levels = sorted(
        DB.FilteredElementCollector(doc).OfClass(DB.Level).ToElements(),
        key=lambda lvl: lvl.Elevation,
    )
    if not levels:
        forms.alert("This project has no Levels.", exitscript=True)

    chosen = levels[0]
    for lvl in levels:
        if lvl.Elevation <= elevation_ft:
            chosen = lvl
        else:
            break
    return chosen


def run_in_subtransaction(func, *args):
    sub = DB.SubTransaction(doc)
    sub.Start()
    try:
        result = func(*args)
        doc.Regenerate()
        sub.Commit()
        return result, None
    except Exception as ex:
        sub.RollBack()
        return None, str(ex)


def connector_direction(connector):
    return connector.CoordinateSystem.BasisZ


def same_size(c1, c2):
    if c1.Shape != c2.Shape:
        return False
    if c1.Shape == DB.ConnectorProfileType.Round:
        return abs(c1.Radius - c2.Radius) < 0.001
    return abs(c1.Width - c2.Width) < 0.001 and abs(c1.Height - c2.Height) < 0.001


def place_two_way(connectors):
    c1, c2 = connectors
    d1, d2 = connector_direction(c1), connector_direction(c2)
    if is_parallel(d1, d2):
        if same_size(c1, c2):
            return doc.Create.NewUnionFitting(c1, c2), "Union"
        return doc.Create.NewTransitionFitting(c1, c2), "Transition"
    return doc.Create.NewElbowFitting(c1, c2), "Elbow"


def place_tee(connectors):
    for i, j in ((0, 1), (0, 2), (1, 2)):
        k = ({0, 1, 2} - {i, j}).pop()
        if is_parallel(connector_direction(connectors[i]), connector_direction(connectors[j])):
            return (
                doc.Create.NewTeeFitting(connectors[i], connectors[j], connectors[k]),
                "Tee",
            )
    return doc.Create.NewTeeFitting(*connectors), "Tee"


def place_cross(connectors):
    dirs = [connector_direction(c) for c in connectors]
    for j in (1, 2, 3):
        if is_parallel(dirs[0], dirs[j]):
            others = [k for k in (1, 2, 3) if k != j]
            return (
                doc.Create.NewCrossFitting(
                    connectors[0], connectors[j], connectors[others[0]], connectors[others[1]]
                ),
                "Cross",
            )
    return doc.Create.NewCrossFitting(*connectors), "Cross"


def place_fitting_for_node(connectors):
    count = len(connectors)
    if count == 2:
        return place_two_way(connectors)
    if count == 3:
        return place_tee(connectors)
    if count == 4:
        return place_cross(connectors)
    raise ValueError(
        "{0} elements meet at this point - only 2, 3 or 4-way junctions supported automatically".format(count)
    )


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main():
    initial_layout = None
    preselected_category = None
    start_measured = False

    while True:
        dialog_mgr = ConfigDialogManager(
            initial_layout=initial_layout,
            preselected_category=preselected_category,
            start_measured=start_measured,
        )
        ui_data = dialog_mgr.show_dialog()

        if dialog_mgr.requested_pick_category:
            # Dialog is fully closed at this point - safe to run an
            # interactive Revit pick, then reopen a fresh dialog prefilled
            # with whatever was captured.
            preselected_category = dialog_mgr.requested_pick_category
            start_measured = True
            layout, capture_error = capture_reference_layout(preselected_category)
            if capture_error:
                forms.alert(capture_error)
                initial_layout = None
                continue
            initial_layout = layout
            continue

        if not ui_data:
            script.exit()

        break

    category_label = ui_data["category_label"]
    elevation_mm = ui_data["elevation_mm"]
    h_gap_mm = ui_data["h_gap_mm"]
    v_gap_mm = ui_data["v_gap_mm"]
    count_h = ui_data["count_h"]
    count_v = ui_data["count_v"]
    grid_config = ui_data["grid_config"]
    layout_mode = ui_data["layout_mode"]
    measured_offsets = ui_data["measured_offsets"]

    cfg = CATEGORY_CONFIG[category_label]
    elevation_ft = mm_to_internal(elevation_mm)

    detail_line_elements = get_detail_lines(category_label)
    if not detail_line_elements:
        forms.alert("No detail lines selected.", exitscript=True)

    segments = []
    skipped = []
    for el in detail_line_elements:
        curve = el.GeometryCurve
        if isinstance(curve, DB.Line):
            segments.append((el, curve))
        else:
            skipped.append(el)

    if not segments:
        forms.alert("No straight detail lines were selected.", exitscript=True)

    anchor_z = segments[0][1].GetEndPoint(0).Z
    delta_z = elevation_ft - anchor_z
    multi_run = count_h > 1 or count_v > 1

    included_positions = [pos for pos, item in grid_config.items() if item["included"]]
    excluded_positions = [pos for pos, item in grid_config.items() if not item["included"]]
    included_count = len(included_positions)

    if layout_mode == "measured":
        # Offsets were measured directly off the existing elements the user
        # selected, so just replicate them - no gap-based math needed.
        h_offsets_ft = {}
        for row in range(count_v):
            h_offsets_ft[row] = [
                measured_offsets[(row, col)]["h_offset_ft"] for col in range(count_h)
            ]
        v_offsets_ft = [
            measured_offsets[(row, 0)]["v_offset_ft"] for row in range(count_v)
        ]
    else:
        h_offsets_ft = {}
        for row in range(count_v):
            h_offsets_ft[row] = [0.0] * count_h
            accum_ft = 0.0
            for col in range(1, count_h):
                prev_size = grid_config[(row, col - 1)]["size"]
                curr_size = grid_config[(row, col)]["size"]

                w_prev_mm = get_dim_mm(prev_size, "width")
                w_curr_mm = get_dim_mm(curr_size, "width")

                step_mm = (w_prev_mm / 2.0) + h_gap_mm + (w_curr_mm / 2.0)
                accum_ft += mm_to_internal(step_mm)
                h_offsets_ft[row][col] = accum_ft

        v_offsets_ft = [0.0] * count_v
        accum_v_ft = 0.0
        for row in range(1, count_v):
            prev_h_mm = max(get_dim_mm(grid_config[(row - 1, c)]["size"], "height") for c in range(count_h))
            curr_h_mm = max(get_dim_mm(grid_config[(row, c)]["size"], "height") for c in range(count_h))

            step_v_mm = (prev_h_mm / 2.0) + v_gap_mm + (curr_h_mm / 2.0)
            accum_v_ft += mm_to_internal(step_v_mm)
            v_offsets_ft[row] = accum_v_ft

    nodes, edges, node_miter_vecs = build_and_orient_graph(segments)

    created_elements = []
    creation_warnings = []
    total_length_ft = 0.0
    levels_used = OrderedDict()

    # Resolve the reference level for each tier BEFORE opening the
    # transaction - pick_level_for_elevation() can exit the script (no
    # levels in project), which must never happen with a transaction open.
    row_levels = {}
    for row in range(count_v):
        if any(grid_config[(row, c)]["included"] for c in range(count_h)):
            row_levels[row] = pick_level_for_elevation(elevation_ft + v_offsets_ft[row])

    fitting_counts = defaultdict(int)
    fitting_warnings = []

    t = DB.Transaction(doc, "Generate {0} Route Bank".format(category_label))
    t.Start()
    try:
        for row in range(count_v):
            if row not in row_levels:
                continue

            tier_z_offset = v_offsets_ft[row]
            level = row_levels[row]
            levels_used[level.Id] = level

            for col in range(count_h):
                run_cfg = grid_config[(row, col)]
                if not run_cfg["included"]:
                    continue

                run_offset_ft = h_offsets_ft[row][col]
                run_label = "Tray R{0}-C{1}".format(row + 1, col + 1) if multi_run else None

                curr_type = run_cfg["type"]
                curr_sys_type = run_cfg["sys_type"]
                curr_size = run_cfg["size"]
                sys_type_id = curr_sys_type.Id if curr_sys_type else DB.ElementId.InvalidElementId

                for edge in edges:
                    start_node = nodes[edge.start_node_key]
                    end_node = nodes[edge.end_node_key]

                    m_start = node_miter_vecs[edge.start_node_key]
                    m_end = node_miter_vecs[edge.end_node_key]

                    p1_base = start_node.point
                    p2_base = end_node.point

                    p1 = DB.XYZ(
                        p1_base.X + m_start.X * run_offset_ft,
                        p1_base.Y + m_start.Y * run_offset_ft,
                        p1_base.Z + delta_z + tier_z_offset,
                    )
                    p2 = DB.XYZ(
                        p2_base.X + m_end.X * run_offset_ft,
                        p2_base.Y + m_end.Y * run_offset_ft,
                        p2_base.Z + delta_z + tier_z_offset,
                    )

                    location = (
                        "{0}, segment near {1}".format(run_label, output.linkify(edge.element.Id))
                        if run_label
                        else "segment near {0}".format(output.linkify(edge.element.Id))
                    )

                    try:
                        new_element = cfg["create"](doc, curr_type.Id, sys_type_id, p1, p2, level.Id)
                    except Exception as ex:
                        creation_warnings.append((location, str(ex)))
                        continue

                    # Record the element as soon as it exists, so a sizing /
                    # comment failure doesn't leave an untracked orphan that
                    # never gets fittings or appears in the report.
                    created_elements.append(new_element)
                    total_length_ft += p1.DistanceTo(p2)

                    try:
                        apply_size(cfg, new_element, curr_size)
                        if run_label:
                            set_param(
                                new_element,
                                DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS,
                                run_label,
                            )
                    except Exception as ex:
                        creation_warnings.append((location, "created, but size not applied: " + str(ex)))

        doc.Regenerate()

        node_map = defaultdict(list)
        for element in created_elements:
            for connector in element.ConnectorManager.Connectors:
                node_map[node_key(connector.Origin)].append(connector)

        for key, connectors in node_map.items():
            if len(connectors) < 2:
                continue
            origin = connectors[0].Origin
            location_str = "({0} mm, {1} mm, {2} mm)".format(
                int(round(internal_to_mm(origin.X))),
                int(round(internal_to_mm(origin.Y))),
                int(round(internal_to_mm(origin.Z))),
            )
            _, error = run_in_subtransaction(place_fitting_for_node, connectors)
            if error:
                fitting_warnings.append((location_str, error))
            elif len(connectors) == 2:
                fitting_counts["Elbow/Union/Transition"] += 1
            elif len(connectors) == 3:
                fitting_counts["Tee"] += 1
            elif len(connectors) == 4:
                fitting_counts["Cross"] += 1

        t.Commit()
    except Exception as ex:
        # Never leave a dangling open transaction - roll back everything.
        if t.GetStatus() == DB.TransactionStatus.Started:
            t.RollBack()
        logger.error("Route generation failed: {0}".format(ex))
        forms.alert(
            "Route generation failed and all changes were rolled back.\n\n{0}".format(ex),
            exitscript=True,
        )

    output.print_md("## {0} Route Summary".format(category_label))
    output.print_md("**Category:** {0}".format(category_label))
    output.print_md(
        "**Layout source:** {0}".format(
            "Matched from existing elements" if layout_mode == "measured" else "Manual grid"
        )
    )
    output.print_md(
        "**Reference level(s):** {0}".format(
            ", ".join(safe_name(lvl) for lvl in levels_used.values())
        )
    )
    output.print_md(
        "**Arrangement:** {0} horizontal x {1} vertical grid ({2} defined) - "
        "**{3} run(s) modeled**, {4} skipped".format(
            count_h, count_v, count_h * count_v, included_count, len(excluded_positions)
        )
    )

    output.print_md("### Run Configurations")
    for (row, col), cfg_item in grid_config.items():
        status = "" if cfg_item["included"] else " _(skipped - not modeled)_"
        output.print_md(
            "- **Tier {0}, Run {1}:** {2} | Size: {3}{4}".format(
                row + 1, col + 1, type_name(cfg_item["type"]), format_size(cfg_item["size"]), status
            )
        )

    output.print_md("**Straight segments created:** {0}".format(len(created_elements)))
    output.print_md("**Total length created:** {0}".format(format_length_mm(total_length_ft)))

    total_fittings = sum(fitting_counts.values())
    output.print_md("**Fittings added:** {0}".format(total_fittings))
    for kind, count in fitting_counts.items():
        output.print_md("- {0}: {1}".format(kind, count))

    if skipped:
        output.print_md("### Skipped elements (not straight lines)")
        for el in skipped:
            output.print_md("- {0}".format(output.linkify(el.Id)))

    if creation_warnings:
        output.print_md("### Segments that could not be created")
        for location, reason in creation_warnings:
            output.print_md("- {0}: {1}".format(location, reason))

    if fitting_warnings:
        output.print_md("### Fittings that could not be placed automatically")
        for location, reason in fitting_warnings:
            output.print_md("- **{0}**: {1}".format(location, reason))

    issue_lines = []
    for el in skipped:
        issue_lines.append("Skipped (not a straight line): {0}".format(safe_name(el)))
    for location, reason in creation_warnings:
        issue_lines.append("Segment not created near {0}: {1}".format(location, reason))
    for location, reason in fitting_warnings:
        issue_lines.append("Fitting not placed at {0}: {1}".format(location, reason))

    summary_data = {
        "category_label": category_label,
        "layout_source_label": (
            "Matched from existing elements" if layout_mode == "measured" else "Manual grid"
        ),
        "arrangement_text": "{0}h x {1}v grid ({2} defined)".format(
            count_h, count_v, count_h * count_v
        ),
        "included_count": included_count,
        "segment_count": len(created_elements),
        "length_text": format_length_mm(total_length_ft),
        "fitting_count": sum(fitting_counts.values()),
        "issue_lines": issue_lines,
    }
    SummaryDialog(summary_data).show()


if __name__ == "__main__":
    main()