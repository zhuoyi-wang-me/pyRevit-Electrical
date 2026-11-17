# -*- coding: utf-8 -*-
__persistentengine__ = True
"""Modeless linked-room 3D view creator for pyRevit.

The window only gathers choices. All Revit API writes run through ExternalEvent,
which keeps the modeless UI safe and compatible with Revit's API threading rules.
"""
from pyrevit import revit, forms
from Autodesk.Revit.DB import (BuiltInCategory, BuiltInParameter, BoundingBoxXYZ, ElementId,
                               FilteredElementCollector, RevitLinkInstance,
                               Transaction, View3D, ViewFamilyType, ViewFamily,
                               XYZ)
from Autodesk.Revit.UI import IExternalEventHandler, ExternalEvent


MM_PER_FOOT = 304.8



def _id_int(element_id):
    """ElementId -> int in every Revit version: .Value (2024+) - .IntegerValue was REMOVED in Revit 2026."""
    value = getattr(element_id, 'Value', None)
    return int(element_id.IntegerValue if value is None else value)


class LinkItem(object):
    def __init__(self, instance):
        self.Instance = instance
        self.Name = instance.Name


class RoomItem(object):
    def __init__(self, room):
        self.RoomId = room.Id
        self.Number = room.Number or ""
        self.Name = room.get_Parameter(BuiltInParameter.ROOM_NAME).AsString() or ""
        self.Level = room.Level.Name if room.Level else ""


def get_loaded_links(doc):
    links = FilteredElementCollector(doc).OfClass(RevitLinkInstance).ToElements()
    return [LinkItem(link) for link in links if link.GetLinkDocument()]


def get_rooms(link_doc):
    rooms = FilteredElementCollector(link_doc).OfCategory(BuiltInCategory.OST_Rooms) \
        .WhereElementIsNotElementType().ToElements()
    return sorted([RoomItem(room) for room in rooms if room.Area > 0],
                  key=lambda item: (item.Number, item.Name))


def clean_name(value):
    invalid = '\\/:*?\"<>|{}[]'
    return ''.join('_' if c in invalid else c for c in value).strip()


def unique_view_name(doc, requested):
    existing = set(v.Name for v in FilteredElementCollector(doc).OfClass(View3D)
                   if not v.IsTemplate)
    name = requested
    count = 2
    while name in existing:
        name = '{} ({})'.format(requested, count)
        count += 1
    return name


def expanded_box(room_box, offset):
    """Return a link-coordinate section box expanded 100 mm in every direction."""
    box = BoundingBoxXYZ()
    box.Min = XYZ(room_box.Min.X - offset, room_box.Min.Y - offset,
                  room_box.Min.Z - offset)
    box.Max = XYZ(room_box.Max.X + offset, room_box.Max.Y + offset,
                  room_box.Max.Z + offset)
    box.Transform = room_box.Transform
    return box


def is_right_handed(transform):
    """BoundingBoxXYZ cannot use a mirrored transform."""
    return transform.BasisX.CrossProduct(transform.BasisY).DotProduct(transform.BasisZ) > 0


def host_axis_aligned_box(room_box, transform, offset):
    """Fallback for mirrored links: transform all corners, then bound them in host axes."""
    points = []
    for x in (room_box.Min.X, room_box.Max.X):
        for y in (room_box.Min.Y, room_box.Max.Y):
            for z in (room_box.Min.Z, room_box.Max.Z):
                points.append(transform.OfPoint(room_box.Transform.OfPoint(XYZ(x, y, z))))
    box = BoundingBoxXYZ()
    box.Min = XYZ(min(p.X for p in points) - offset,
                  min(p.Y for p in points) - offset,
                  min(p.Z for p in points) - offset)
    box.Max = XYZ(max(p.X for p in points) + offset,
                  max(p.Y for p in points) + offset,
                  max(p.Z for p in points) + offset)
    return box


class CreateViewsHandler(IExternalEventHandler):
    def __init__(self, window):
        self.window = window
        self.request = None

    def GetName(self):
        return 'Create linked room 3D views'

    def Execute(self, uiapp):
        # Do not read WPF selection here. The click handler captures it before
        # raising the event, avoiding stale UI state and cross-context failures.
        data = self.request
        self.request = None
        if not data:
            self.window.set_status('No creation request was received. Select rooms and try again.')
            return
        doc, link_instance, room_ids, prefix = data
        active_doc = uiapp.ActiveUIDocument.Document if uiapp.ActiveUIDocument else None
        if active_doc != doc:
            self.window.set_status('Return to the host project used to open this window, then try again.')
            return
        link_doc = link_instance.GetLinkDocument()
        if not link_doc:
            self.window.set_status('The selected link is no longer loaded.')
            return
        view_type = next((vft for vft in FilteredElementCollector(doc).OfClass(ViewFamilyType)
                          if vft.ViewFamily == ViewFamily.ThreeDimensional), None)
        if not view_type:
            self.window.set_status('No 3D view family type is available in this project.')
            return

        try:
            transform = link_instance.GetTotalTransform()
            made, skipped = 0, []
            tx = Transaction(doc, 'Create linked room 3D views')
            tx.Start()
            for room_id in room_ids:
                room = link_doc.GetElement(room_id)
                bbox = room.get_BoundingBox(None) if room else None
                if not bbox:
                    skipped.append(str(_id_int(room_id)))
                    continue
                view = View3D.CreateIsometric(doc, view_type.Id)
                # The link transform preserves the room's orientation, including rotation.
                if is_right_handed(transform):
                    room_section_box = expanded_box(bbox, 100.0 / MM_PER_FOOT)
                    room_section_box.Transform = transform.Multiply(room_section_box.Transform)
                else:
                    room_section_box = host_axis_aligned_box(bbox, transform, 100.0 / MM_PER_FOOT)
                view.SetSectionBox(room_section_box)
                requested_name = clean_name('{}{} {}'.format(prefix, room.Number, room.Name))
                view.Name = unique_view_name(doc, requested_name or 'Linked Room 3D')
                made += 1
            tx.Commit()
        except Exception as exc:
            if 'tx' in locals() and tx.GetStatus().ToString() == 'Started':
                tx.RollBack()
            self.window.set_status('No views were created: {}'.format(exc))
            return
        message = 'Created {} 3D view(s), each with a 100 mm room offset.'.format(made)
        if skipped:
            message += ' Skipped {} room(s) without a bounding box.'.format(len(skipped))
        self.window.set_status(message)


class LinkedRoomsWindow(forms.WPFWindow):
    def __init__(self):
        forms.WPFWindow.__init__(self, 'CreateLinkedRoom3D.xaml')
        self.handler = CreateViewsHandler(self)
        self.external_event = ExternalEvent.Create(self.handler)
        self.all_rooms = []
        self.linkCombo.ItemsSource = get_loaded_links(revit.doc)
        if self.linkCombo.Items.Count:
            self.linkCombo.SelectedIndex = 0
            self.load_rooms()
        else:
            self.set_status('No loaded Revit links were found.')
        self.linkCombo.SelectionChanged += self.link_changed
        self.searchText.TextChanged += self.filter_rooms
        self.refreshButton.Click += self.refresh_rooms
        self.createButton.Click += self.create_views
        self.closeButton.Click += self.close_window

    def set_status(self, message):
        self.statusText.Text = message

    def load_rooms(self):
        item = self.linkCombo.SelectedItem
        if not item:
            self.roomsList.ItemsSource = []
            return
        self.all_rooms = get_rooms(item.Instance.GetLinkDocument())
        self.apply_room_filter()

    def apply_room_filter(self):
        query = (self.searchText.Text or '').strip().lower()
        if query:
            rooms = [room for room in self.all_rooms if query in
                     '{} {} {}'.format(room.Number, room.Name, room.Level).lower()]
        else:
            rooms = self.all_rooms
        self.roomsList.ItemsSource = rooms
        self.set_status('{} of {} placed room(s) shown. Select one or more rooms.'.format(
            len(rooms), len(self.all_rooms)))

    def link_changed(self, sender, args):
        self.load_rooms()

    def refresh_rooms(self, sender, args):
        self.load_rooms()

    def filter_rooms(self, sender, args):
        self.apply_room_filter()

    def get_request(self):
        link = self.linkCombo.SelectedItem
        selected = list(self.roomsList.SelectedItems)
        if not link:
            self.set_status('Select a loaded linked model first.')
            return None
        if not selected:
            self.set_status('Select at least one room.')
            return None
        return revit.doc, link.Instance, [item.RoomId for item in selected], self.prefixText.Text or ''

    def create_views(self, sender, args):
        request = self.get_request()
        if request:
            self.handler.request = request
            result = self.external_event.Raise()
            self.set_status('Creation request sent ({}).'.format(result))

    def close_window(self, sender, args):
        self.Close()


window = LinkedRoomsWindow()
window.Show()
