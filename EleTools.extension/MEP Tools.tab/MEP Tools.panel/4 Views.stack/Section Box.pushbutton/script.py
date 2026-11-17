# -*- coding: utf-8 -*-
"""3D Section Box by Element Selection.

Creates / updates a 3D view Section Box oriented to one or more selected
elements (from the host model or a linked model).

Compatible with pyRevit, Revit 2024 / 2025 / 2026, IronPython 2.7.

ARCHITECTURE NOTE (read this if "Select elements" ever appears to do
nothing): the UI window below is shown MODALLY (WPFWindow.show(modal=True),
i.e. ShowDialog()). This is required, not optional. A modeless window
returns control to Revit the instant .show() is called, so any button
click handled afterward runs OUTSIDE a valid Revit API execution context -
Selection.PickObjects() and Transaction.Start() will fail from there.
Showing the window modally keeps the whole call stack, including button
clicks, nested inside this command's own valid API context for as long as
the window is open; self.Hide() still lets Revit's main window receive
clicks normally while the user picks elements.
"""
from __future__ import division

import os
import math
import traceback

from pyrevit import revit, DB, UI, forms, script

from Autodesk.Revit.DB import (
    XYZ, Transform, BoundingBoxXYZ, Line,
    ViewFamily, ViewFamilyType, View3D,
    Transaction, TransactionStatus,
    LocationCurve, LocationPoint,
    RevitLinkInstance, Options,
    ElementId, BuiltInCategory
)
from Autodesk.Revit.UI.Selection import ObjectType, ISelectionFilter
from Autodesk.Revit.Exceptions import OperationCanceledException

logger = script.get_logger()
output = script.get_output()

doc = revit.doc
uidoc = revit.uidoc

MIN_AXIS_THICKNESS_FT = 1.0
COLLAPSED_AXIS_TOLERANCE_FT = 0.05


# ----------------------------------------------------------------------
# Selection filters
# ----------------------------------------------------------------------
class HostElementFilter(ISelectionFilter):
    def AllowElement(self, element):
        return True

    def AllowReference(self, reference, position):
        return False


class LinkedElementFilter(ISelectionFilter):
    def AllowElement(self, element):
        return True

    def AllowReference(self, reference, position):
        return True


class PickedElement(object):
    def __init__(self, element, source_doc, link_instance):
        self.element = element
        self.source_doc = source_doc
        self.link_instance = link_instance
        self.is_linked = link_instance is not None

    @property
    def host_transform(self):
        if self.is_linked:
            return self.link_instance.GetTotalTransform()
        return Transform.Identity


def _show_error(title, message):
    """Never swallow real errors silently - always surface them."""
    logger.error("{}: {}".format(title, message))
    forms.alert(message, title=title)


# ----------------------------------------------------------------------
# 1. Selection
# ----------------------------------------------------------------------
def get_selected_elements(uidoc, from_link):
    sel = uidoc.Selection
    picked = []

    try:
        if from_link:
            refs = sel.PickObjects(
                ObjectType.LinkedElement,
                LinkedElementFilter(),
                "Select elements to crop (host or linked), then click Finish."
            )
            if not refs:
                return None
            for ref in refs:
                el = doc.GetElement(ref.ElementId)
                if isinstance(el, RevitLinkInstance):
                    link_doc = el.GetLinkDocument()
                    if link_doc is None:
                        _show_error(
                            "Link Unloaded",
                            "The link '{}' is unloaded and cannot be used. "
                            "Please reload the link and try again.".format(el.Name))
                        return None
                    lelem = link_doc.GetElement(ref.LinkedElementId)
                    if lelem is not None:
                        picked.append(PickedElement(lelem, link_doc, el))
                else:
                    if el is not None:
                        picked.append(PickedElement(el, doc, None))
        else:
            refs = sel.PickObjects(
                ObjectType.Element,
                HostElementFilter(),
                "Select elements to crop, then click Finish."
            )
            if not refs:
                return None
            for ref in refs:
                elem = doc.GetElement(ref.ElementId)
                if elem is not None:
                    picked.append(PickedElement(elem, doc, None))

    except OperationCanceledException:
        return None
    except Exception as ex:
        _show_error("Selection Error",
                     "Element selection failed:\n{}".format(traceback.format_exc()))
        return None

    if not picked:
        return None
    return picked


def get_single_orientation_element(uidoc):
    sel = uidoc.Selection
    try:
        ref = sel.PickObject(
            ObjectType.LinkedElement,
            LinkedElementFilter(),
            "Select an element to orient the 3D view (Esc to cancel)."
        )
    except OperationCanceledException:
        return None
    except Exception as ex:
        _show_error("Selection Error",
                     "Orientation-element selection failed:\n{}".format(traceback.format_exc()))
        return None

    if ref is None:
        return None

    el = doc.GetElement(ref.ElementId)
    if isinstance(el, RevitLinkInstance):
        link_doc = el.GetLinkDocument()
        if link_doc is None:
            _show_error("Link Unloaded",
                         "The link is unloaded and cannot be used for orientation.")
            return None
        lelem = link_doc.GetElement(ref.LinkedElementId)
        if lelem is None:
            return None
        return PickedElement(lelem, link_doc, el)
    else:
        return PickedElement(el, doc, None)


# ----------------------------------------------------------------------
# 2. Orientation
# ----------------------------------------------------------------------
def get_element_id_value(element_id):
    """Return an ElementId's numeric value across Revit API versions.

    Revit 2024 switched ElementId's backing type from 32-bit int to
    64-bit long as part of extensible storage / large-model support.
    The old `.IntegerValue` property was deprecated in favor of `.Value`
    (added in 2024) and is removed entirely in some later API surfaces -
    that mismatch is exactly what raised:
        AttributeError: 'ElementId' object has no attribute 'IntegerValue'
    Try the modern property first, fall back to the legacy one for older
    Revit versions, and fall back to str() as a last resort so this never
    raises regardless of API version.
    """
    if element_id is None:
        return None
    try:
        return element_id.Value
    except AttributeError:
        pass
    try:
        return element_id.IntegerValue
    except AttributeError:
        pass
    return str(element_id)


def _normalize(v):
    length = v.GetLength()
    if length < 1e-9:
        return None
    return v.Divide(length)


def _orthonormal_basis_from_x_and_up(x_axis, up_hint=XYZ.BasisZ):
    x_axis = _normalize(x_axis)
    if x_axis is None:
        return None
    up = up_hint
    if abs(x_axis.DotProduct(up)) > 0.999:
        up = XYZ.BasisY if abs(x_axis.DotProduct(XYZ.BasisY)) < 0.999 else XYZ.BasisX
    y_axis = _normalize(up.CrossProduct(x_axis))
    if y_axis is None:
        return None
    z_axis = x_axis.CrossProduct(y_axis)
    return x_axis, y_axis, z_axis


def get_element_orientation(picked):
    """Returns (basis_tuple, strategy_name, notes[])."""
    element = picked.element
    notes = []

    try:
        is_wall = isinstance(element, DB.Wall)
        notes.append("isinstance Wall = {}".format(is_wall))
        if is_wall:
            loc = element.Location
            if isinstance(loc, LocationCurve):
                curve = loc.Curve
                direction = curve.GetEndPoint(1) - curve.GetEndPoint(0)
                basis = _orthonormal_basis_from_x_and_up(direction)
                if basis:
                    return basis, "wall-location-curve", notes
    except Exception as ex:
        notes.append("Wall branch raised: {}".format(ex))

    try:
        is_fi = isinstance(element, DB.FamilyInstance)
        notes.append("isinstance FamilyInstance = {}".format(is_fi))
        if is_fi:
            inst_transform = element.GetTransform()
            basis = (
                _normalize(inst_transform.BasisX),
                _normalize(inst_transform.BasisY),
                _normalize(inst_transform.BasisZ),
            )
            if all(b is not None for b in basis):
                x_axis = basis[0]
                z_hint = basis[2] if basis[2] else XYZ.BasisZ
                rebuilt = _orthonormal_basis_from_x_and_up(x_axis, z_hint)
                if rebuilt:
                    return rebuilt, "family-instance-transform", notes
    except Exception as ex:
        notes.append("FamilyInstance branch raised: {}".format(ex))

    try:
        loc = getattr(element, "Location", None)
        if isinstance(loc, LocationCurve):
            curve = loc.Curve
            direction = curve.GetEndPoint(1) - curve.GetEndPoint(0)
            basis = _orthonormal_basis_from_x_and_up(direction)
            if basis:
                return basis, "location-curve", notes
    except Exception as ex:
        notes.append("LocationCurve branch raised: {}".format(ex))

    try:
        mep_model = getattr(element, "MEPModel", None)
        connectors = None
        if mep_model is not None and mep_model.ConnectorManager is not None:
            connectors = list(mep_model.ConnectorManager.Connectors)
        elif hasattr(element, "ConnectorManager") and element.ConnectorManager is not None:
            connectors = list(element.ConnectorManager.Connectors)
        if connectors:
            first = connectors[0]
            direction = first.CoordinateSystem.BasisZ
            basis = _orthonormal_basis_from_x_and_up(direction)
            if basis:
                return basis, "mep-connector", notes
    except Exception as ex:
        notes.append("Connector branch raised: {}".format(ex))

    try:
        loc = getattr(element, "Location", None)
        if isinstance(loc, LocationPoint):
            angle = loc.Rotation
            if angle:
                direction = XYZ(math.cos(angle), math.sin(angle), 0.0)
                basis = _orthonormal_basis_from_x_and_up(direction)
                if basis:
                    return basis, "location-point-rotation", notes
    except Exception as ex:
        notes.append("LocationPoint branch raised: {}".format(ex))

    notes.append("NO strategy matched - falling back to world axes (box will be axis-aligned)")
    return (XYZ.BasisX, XYZ.BasisY, XYZ.BasisZ), "world-axes-fallback", notes


def choose_orientation_reference(picked_elements):
    def score(pe):
        el = pe.element
        try:
            if isinstance(el, DB.Wall):
                return 0
        except Exception:
            pass
        try:
            if isinstance(el, DB.FamilyInstance):
                cat_id = get_element_id_value(el.Category.Id) if el.Category else -1
                structural_cats = (
                    int(BuiltInCategory.OST_StructuralFraming),
                    int(BuiltInCategory.OST_StructuralColumns),
                    int(BuiltInCategory.OST_Columns),
                )
                if cat_id in structural_cats:
                    return 1
                return 2
        except Exception:
            pass
        try:
            if isinstance(el.Location, LocationCurve):
                return 3
        except Exception:
            pass
        return 4

    return sorted(picked_elements, key=score)[0]


def print_orientation_debug(orientation_source, basis_x, basis_y, basis_z, strategy, notes):
    try:
        angle_deg = math.degrees(math.atan2(basis_x.Y, basis_x.X))
    except Exception:
        angle_deg = 0.0

    el = orientation_source.element
    try:
        el_desc = "{} (Id {})".format(el.Category.Name if el.Category else el.GetType().Name,
                                       get_element_id_value(el.Id))
    except Exception:
        el_desc = "Id {}".format(get_element_id_value(el.Id))

    source_label = "LINKED model ({})".format(
        orientation_source.link_instance.Name) if orientation_source.is_linked else "HOST model"

    output.print_md("**Element orientation** (from {} - {}) - strategy used: **{}**".format(
        el_desc, source_label, strategy))
    output.print_md(
        "- X = ({:.4f}, {:.4f}, {:.4f})\n"
        "- Y = ({:.4f}, {:.4f}, {:.4f})\n"
        "- Z = ({:.4f}, {:.4f}, {:.4f})\n"
        "- Angle vs. project X axis = {:.2f} degrees".format(
            basis_x.X, basis_x.Y, basis_x.Z,
            basis_y.X, basis_y.Y, basis_y.Z,
            basis_z.X, basis_z.Y, basis_z.Z,
            angle_deg))
    if strategy == "world-axes-fallback":
        output.print_md("**WARNING: no orientation strategy matched this element - "
                         "the box will be axis-aligned.** Trace:")
    for n in notes:
        output.print_md("  - {}".format(n))


# ----------------------------------------------------------------------
# 3. Bounding box corners & transforms
# ----------------------------------------------------------------------
def get_bounding_box_corners(element):
    try:
        bbox = element.get_BoundingBox(None)
    except Exception:
        bbox = None
    if bbox is None:
        return None

    mn, mx = bbox.Min, bbox.Max
    if (mx.X - mn.X < -1e-9) or (mx.Y - mn.Y < -1e-9) or (mx.Z - mn.Z < -1e-9):
        return None

    local_corners = [
        XYZ(mn.X, mn.Y, mn.Z), XYZ(mx.X, mn.Y, mn.Z),
        XYZ(mx.X, mx.Y, mn.Z), XYZ(mn.X, mx.Y, mn.Z),
        XYZ(mn.X, mn.Y, mx.Z), XYZ(mx.X, mn.Y, mx.Z),
        XYZ(mx.X, mx.Y, mx.Z), XYZ(mn.X, mx.Y, mx.Z),
    ]
    tf = bbox.Transform
    return [tf.OfPoint(pt) for pt in local_corners]


def transform_linked_geometry(points, link_transform):
    if link_transform is None or link_transform.AlmostEqual(Transform.Identity):
        return list(points)
    return [link_transform.OfPoint(p) for p in points]


# ----------------------------------------------------------------------
# 4. Combined oriented bounding box
# ----------------------------------------------------------------------
def calculate_combined_bounds(picked_elements, orientation_basis):
    basis_x, basis_y, basis_z = orientation_basis
    orientation_transform = Transform.Identity
    orientation_transform.Origin = XYZ.Zero
    orientation_transform.BasisX = basis_x
    orientation_transform.BasisY = basis_y
    orientation_transform.BasisZ = basis_z
    inverse = orientation_transform.Inverse

    local_min = None
    local_max = None
    any_geometry = False

    for pe in picked_elements:
        corners = get_bounding_box_corners(pe.element)
        if not corners:
            continue
        host_corners = transform_linked_geometry(corners, pe.host_transform)
        for hp in host_corners:
            lp = inverse.OfPoint(hp)
            if local_min is None:
                local_min = XYZ(lp.X, lp.Y, lp.Z)
                local_max = XYZ(lp.X, lp.Y, lp.Z)
            else:
                local_min = XYZ(min(local_min.X, lp.X), min(local_min.Y, lp.Y), min(local_min.Z, lp.Z))
                local_max = XYZ(max(local_max.X, lp.X), max(local_max.Y, lp.Y), max(local_max.Z, lp.Z))
            any_geometry = True

    if not any_geometry:
        return None, None, None
    return local_min, local_max, orientation_transform


# ----------------------------------------------------------------------
# 5. Offset
# ----------------------------------------------------------------------
def apply_offset(local_min, local_max, offset_percent):
    fraction = max(offset_percent, 0.0) / 100.0
    mins = [local_min.X, local_min.Y, local_min.Z]
    maxs = [local_max.X, local_max.Y, local_max.Z]
    new_min = [0.0, 0.0, 0.0]
    new_max = [0.0, 0.0, 0.0]

    for i in range(3):
        size = maxs[i] - mins[i]
        center = (maxs[i] + mins[i]) / 2.0
        if size < COLLAPSED_AXIS_TOLERANCE_FT:
            size = MIN_AXIS_THICKNESS_FT
        half = (size / 2.0) * (1.0 + fraction)
        new_min[i] = center - half
        new_max[i] = center + half

    return XYZ(new_min[0], new_min[1], new_min[2]), XYZ(new_max[0], new_max[1], new_max[2])


# ----------------------------------------------------------------------
# 6. Build final BoundingBoxXYZ
# ----------------------------------------------------------------------
def build_final_section_box(local_min, local_max, orientation_transform):
    size = local_max - local_min
    world_origin = orientation_transform.OfPoint(local_min)

    box_transform = Transform.Identity
    box_transform.Origin = world_origin
    box_transform.BasisX = orientation_transform.BasisX
    box_transform.BasisY = orientation_transform.BasisY
    box_transform.BasisZ = orientation_transform.BasisZ

    box = BoundingBoxXYZ()
    box.Transform = box_transform
    box.Min = XYZ(0.0, 0.0, 0.0)
    box.Max = XYZ(size.X, size.Y, size.Z)
    return box


def validate_section_box(box):
    if box is None:
        return False, "No bounding box was computed."
    size = box.Max - box.Min
    if size.X <= 0 or size.Y <= 0 or size.Z <= 0:
        return False, "Computed Section Box has a zero or negative dimension."
    tf = box.Transform
    if tf.BasisX.GetLength() < 1e-6 or tf.BasisY.GetLength() < 1e-6 or tf.BasisZ.GetLength() < 1e-6:
        return False, "Computed Section Box transform is degenerate."
    return True, ""


# ----------------------------------------------------------------------
# 7. 3D view handling
# ----------------------------------------------------------------------
def get_target_view_name():
    try:
        username = doc.Application.Username
    except Exception:
        username = "user"
    if not username:
        username = "user"
    safe_user = "".join(ch for ch in username if ch not in '\\:{}[]|;<>?`')
    return "3D-{}".format(safe_user)


def find_existing_view_by_name(doc, name):
    collector = DB.FilteredElementCollector(doc).OfClass(View3D)
    for v in collector:
        if not v.IsTemplate and v.Name == name:
            return v
    return None


def create_or_get_3d_view(doc, uidoc):
    active_view = uidoc.ActiveView
    if isinstance(active_view, View3D) and not active_view.IsTemplate:
        return active_view, False

    target_name = get_target_view_name()
    existing = find_existing_view_by_name(doc, target_name)
    if existing is not None:
        return existing, False

    view_family_type = None
    for vft in DB.FilteredElementCollector(doc).OfClass(ViewFamilyType):
        if vft.ViewFamily == ViewFamily.ThreeDimensional:
            view_family_type = vft
            break
    if view_family_type is None:
        return None, False

    new_view = View3D.CreateIsometric(doc, view_family_type.Id)
    try:
        new_view.Name = target_name
    except Exception:
        try:
            new_view.Name = target_name + "-" + str(get_element_id_value(new_view.Id))
        except Exception:
            pass

    return new_view, True


def apply_section_box(view, box, enable):
    """IMPORTANT #1: View3D.SectionBox has not been a settable property
    since Revit 2014 - use SetSectionBox()/GetSectionBox() only.
    IMPORTANT #2 (ORDER): activate IsSectionBoxActive BEFORE calling
    SetSectionBox() - turning the option on for the first time can make
    Revit re-initialize the box to its own default extents, silently
    overwriting a box set beforehand."""
    if enable:
        view.IsSectionBoxActive = True
        if box is not None:
            view.SetSectionBox(box)
    else:
        if box is not None:
            view.SetSectionBox(box)
        view.IsSectionBoxActive = False

    try:
        applied = view.GetSectionBox()
        tf = applied.Transform
        output.print_md(
            "**Applied Section Box (read back from the view)**\n"
            "- BasisX = ({:.4f}, {:.4f}, {:.4f})\n"
            "- BasisY = ({:.4f}, {:.4f}, {:.4f})\n"
            "- BasisZ = ({:.4f}, {:.4f}, {:.4f})\n"
            "- IsSectionBoxActive = {}".format(
                tf.BasisX.X, tf.BasisX.Y, tf.BasisX.Z,
                tf.BasisY.X, tf.BasisY.Y, tf.BasisY.Z,
                tf.BasisZ.X, tf.BasisZ.Y, tf.BasisZ.Z,
                view.IsSectionBoxActive))
    except Exception as ex:
        logger.debug("Could not read back section box for debug output: {}".format(ex))


# ----------------------------------------------------------------------
# Main workflow
# ----------------------------------------------------------------------
def run_section_box_workflow(from_link, manual_orientation, offset_percent, section_box_on):
    picked_elements = get_selected_elements(uidoc, from_link)
    if not picked_elements:
        forms.alert("No elements were selected. The Section Box was not changed.",
                    title="Nothing Selected")
        return

    if manual_orientation:
        orientation_source = get_single_orientation_element(uidoc)
        if orientation_source is None:
            forms.alert("Orientation selection was cancelled. The Section Box was not changed.",
                        title="Cancelled")
            return
    else:
        if len(picked_elements) == 1:
            orientation_source = picked_elements[0]
        else:
            orientation_source = choose_orientation_reference(picked_elements)

    local_basis, strategy, strategy_notes = get_element_orientation(orientation_source)
    host_tf = orientation_source.host_transform
    basis_x = _normalize(host_tf.OfVector(local_basis[0]))
    basis_y = _normalize(host_tf.OfVector(local_basis[1]))
    basis_z = _normalize(host_tf.OfVector(local_basis[2]))
    if not (basis_x and basis_y and basis_z):
        basis_x, basis_y, basis_z = XYZ.BasisX, XYZ.BasisY, XYZ.BasisZ
        strategy_notes.append("host-transform application produced a degenerate vector - "
                               "forced back to world axes")
    else:
        rebuilt = _orthonormal_basis_from_x_and_up(basis_x, basis_z)
        if rebuilt:
            basis_x, basis_y, basis_z = rebuilt

    print_orientation_debug(orientation_source, basis_x, basis_y, basis_z, strategy, strategy_notes)

    local_min, local_max, orientation_transform = calculate_combined_bounds(
        picked_elements, (basis_x, basis_y, basis_z))

    if local_min is None:
        forms.alert(
            "None of the selected elements have a usable bounding box.\n"
            "The Section Box was not changed.",
            title="No Usable Geometry")
        return

    output.print_md(
        "**Local-frame extents (before offset)**\n"
        "- min = ({:.3f}, {:.3f}, {:.3f})\n"
        "- max = ({:.3f}, {:.3f}, {:.3f})\n"
        "- size = ({:.3f}, {:.3f}, {:.3f})".format(
            local_min.X, local_min.Y, local_min.Z,
            local_max.X, local_max.Y, local_max.Z,
            local_max.X - local_min.X, local_max.Y - local_min.Y, local_max.Z - local_min.Z))

    padded_min, padded_max = apply_offset(local_min, local_max, offset_percent)
    final_box = build_final_section_box(padded_min, padded_max, orientation_transform)

    output.print_md(
        "**Final box (about to be sent to Revit)**\n"
        "- Transform.Origin = ({:.3f}, {:.3f}, {:.3f})\n"
        "- Transform.BasisX = ({:.4f}, {:.4f}, {:.4f})\n"
        "- box.Min (local) = ({:.3f}, {:.3f}, {:.3f})\n"
        "- box.Max (local) = ({:.3f}, {:.3f}, {:.3f})".format(
            final_box.Transform.Origin.X, final_box.Transform.Origin.Y, final_box.Transform.Origin.Z,
            final_box.Transform.BasisX.X, final_box.Transform.BasisX.Y, final_box.Transform.BasisX.Z,
            final_box.Min.X, final_box.Min.Y, final_box.Min.Z,
            final_box.Max.X, final_box.Max.Y, final_box.Max.Z))

    is_valid, reason = validate_section_box(final_box)
    if not is_valid:
        forms.alert("Could not build a valid Section Box: {}".format(reason),
                    title="Invalid Section Box")
        return

    t = Transaction(doc, "3D Section Box by Selection")
    try:
        t.Start()
        view, is_new = create_or_get_3d_view(doc, uidoc)
        if view is None:
            t.RollBack()
            forms.alert("Could not create or find a 3D view to apply the Section Box to.",
                        title="View Error")
            return

        apply_section_box(view, final_box, section_box_on)

        t.Commit()
        if t.GetStatus() != TransactionStatus.Committed:
            forms.alert("Revit could not commit the changes.", title="Transaction Failed")
            return

    except Exception as ex:
        if t.HasStarted() and not t.HasEnded():
            t.RollBack()
        _show_error("Error", "An error occurred while applying the Section Box:\n{}".format(
            traceback.format_exc()))
        return

    try:
        uidoc.ActiveView = view
        uidoc.RefreshActiveView()
    except Exception:
        pass


# ----------------------------------------------------------------------
# UI
# ----------------------------------------------------------------------
class SectionBoxUI(forms.WPFWindow):
    def __init__(self, xaml_file):
        forms.WPFWindow.__init__(self, xaml_file)

    def slider_offset_ValueChanged(self, sender, args):
        try:
            value = int(round(self.slider_offset.Value))
            if self.txt_offset.Text != str(value):
                self.txt_offset.Text = str(value)
        except Exception:
            pass

    def txt_offset_TextChanged(self, sender, args):
        text = self.txt_offset.Text.strip()
        if text == "":
            return
        try:
            value = int(float(text))
        except ValueError:
            return
        value = max(0, min(100, value))
        if abs(self.slider_offset.Value - value) > 0.01:
            self.slider_offset.Value = value

    def btn_plus_Click(self, sender, args):
        self.slider_offset.Value = min(100, self.slider_offset.Value + 1)

    def btn_minus_Click(self, sender, args):
        self.slider_offset.Value = max(0, self.slider_offset.Value - 1)

    def btn_cancel_Click(self, sender, args):
        self.Close()

    def btn_select_Click(self, sender, args):
        from_link = bool(self.radio_in_link.IsChecked)
        manual_orientation = bool(self.chk_manual_orientation.IsChecked)
        section_box_on = bool(self.chk_section_box.IsChecked)
        try:
            offset_percent = float(self.txt_offset.Text)
        except Exception:
            offset_percent = 0.0

        # Hide (not Close) so the modal window's API context stays alive
        # for the duration of the pick + transaction below.
        self.Hide()
        try:
            run_section_box_workflow(from_link, manual_orientation,
                                      offset_percent, section_box_on)
        except Exception as ex:
            _show_error("Unexpected Error",
                        "The Section Box tool hit an unexpected error:\n{}".format(
                            traceback.format_exc()))
        finally:
            self.Close()


# ----------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------
if __name__ == "__main__":
    xaml_path = os.path.join(os.path.dirname(__file__), "ui.xaml")
    # MUST be modal=True - see the ARCHITECTURE NOTE in the module
    # docstring above for why a modeless window breaks Revit API calls
    # made from its button-click handlers.
    SectionBoxUI(xaml_path).show(modal=True)
