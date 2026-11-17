# -*- coding: utf-8 -*-
"""
elecplace.linkdata
==================
Reads the architectural link: loaded link instances, their transforms,
and every Room with its number, name, level, boundary and bounding walls.

Revit API used:
  DB.FilteredElementCollector, DB.RevitLinkInstance.GetLinkDocument(),
  DB.RevitLinkInstance.GetTotalTransform(), DB.BuiltInCategory.OST_Rooms,
  DB.Architecture.Room (Number, Level, Area, Location, BaseOffset,
  GetBoundarySegments, IsPointInRoom), DB.SpatialElementBoundaryOptions,
  DB.SpatialElementBoundaryLocation, DB.BoundarySegment, DB.Wall
"""
from Autodesk.Revit import DB
from Autodesk.Revit.DB.Architecture import Room

from elecplace.compat import eid_int, element_name, param_as_string, to_text


class LinkInfo(object):
    """A loaded link instance plus everything needed to map its coordinates."""

    def __init__(self, instance):
        self.instance = instance
        self.doc = instance.GetLinkDocument()
        # Total transform = link position/rotation relative to the host.
        # Every point/vector read from the link goes through this.
        self.transform = instance.GetTotalTransform()
        self.id = eid_int(instance.Id)
        self.title = to_text(self.doc.Title)
        self.name = u"{}  [link instance {}]".format(self.title, self.id)


def get_loaded_links(doc):
    """
    Return (loaded_links, unloaded_count).
    Each placed instance of a link is returned separately, because two
    instances of the same file can sit at different positions.
    """
    loaded, unloaded = [], 0
    for inst in DB.FilteredElementCollector(doc).OfClass(DB.RevitLinkInstance):
        try:
            ldoc = inst.GetLinkDocument()
        except Exception:
            ldoc = None
        if ldoc is None:
            unloaded += 1
            continue
        loaded.append(LinkInfo(inst))
    loaded.sort(key=lambda li: li.name)
    return loaded, unloaded


class RoomInfo(object):
    """Room data extracted from the link (all values in LINK coordinates)."""

    def __init__(self, room, link):
        self.room = room
        self.link = link
        self.uid = room.UniqueId
        self.id = eid_int(room.Id)
        self.number = to_text(room.Number)
        self.name = to_text(param_as_string(room, DB.BuiltInParameter.ROOM_NAME))
        level = room.Level
        self.level_name = to_text(element_name(level)) if level else u"<no level>"
        # ProjectElevation = elevation relative to the model's internal
        # origin, i.e. the same Z system as the link geometry, regardless of
        # the level type's "Elevation Base" setting.
        self.level_elev = level.ProjectElevation if level else 0.0
        try:
            self.base_offset = room.BaseOffset
        except Exception:
            self.base_offset = 0.0
        self.area = room.Area         # internal ft2 - drives room-size quantities
        try:
            self.height = room.UnboundedHeight
        except Exception:
            self.height = 10.0
        try:
            bb = room.get_BoundingBox(None)
            self.bbox = (bb.Min, bb.Max) if bb else None
        except Exception:
            self.bbox = None
        self.loops = []               # list[list[BoundarySegment]]
        self.bounding_wall_ids = set()
        self.perimeter = 0.0

    def ffl(self, include_base_offset=False):
        """Finished Floor Level (link Z, feet)."""
        return self.level_elev + (self.base_offset if include_base_offset else 0.0)

    @property
    def label(self):
        return u"{} - {}".format(self.number or u"?", self.name or u"<unnamed>")

    @property
    def full_label(self):
        return u"{}  [{}]".format(self.label, self.level_name)


def collect_rooms(link, settings, logger=None):
    """
    Read every Room in the link.

    returns (valid_rooms, skipped) where skipped = [(RoomInfo, reason)].
    Only rooms that are PLACED (Location != None) and ENCLOSED (Area > 0)
    with at least one boundary loop are considered valid. Area == 0 covers
    both 'Not Enclosed' and 'Redundant' rooms.
    """
    opts = DB.SpatialElementBoundaryOptions()
    opts.SpatialElementBoundaryLocation = (
        DB.SpatialElementBoundaryLocation.Finish if settings.use_finish_boundary
        else DB.SpatialElementBoundaryLocation.Center)

    valid, skipped = [], []
    collector = (DB.FilteredElementCollector(link.doc)
                 .OfCategory(DB.BuiltInCategory.OST_Rooms)
                 .WhereElementIsNotElementType())

    for room in collector:
        if not isinstance(room, Room):
            continue
        info = RoomInfo(room, link)
        try:
            if room.Location is None:
                skipped.append((info, u"Not placed"))
                continue
            if room.Area <= 1e-6:
                skipped.append((info, u"Not enclosed or redundant (area = 0)"))
                continue
            loops = room.GetBoundarySegments(opts)
            if loops is None or loops.Count == 0:
                skipped.append((info, u"No boundary segments returned"))
                continue

            for loop in loops:
                seg_list = list(loop)
                info.loops.append(seg_list)
                for seg in seg_list:
                    try:
                        info.perimeter += seg.GetCurve().Length
                    except Exception:
                        pass
                    element = link.doc.GetElement(seg.ElementId)
                    if isinstance(element, DB.Wall):
                        info.bounding_wall_ids.add(eid_int(element.Id))

            if not info.bounding_wall_ids:
                skipped.append((info, u"No bounding walls (only separation lines / columns)"))
                continue
            valid.append(info)
        except Exception as ex:
            if logger:
                logger.debug("Room {} failed: {}".format(info.id, ex))
            skipped.append((info, u"Error reading room: {}".format(to_text(ex))))

    valid.sort(key=lambda r: (r.level_elev, r.number, r.name))
    return valid, skipped
