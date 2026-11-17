# -*- coding: utf-8 -*-
"""Data mapping templates outside Revit. Run: python tests/test_datamap.py"""
import os, sys, types
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_equipgeom            # installs Revit / System mocks   # noqa: F401
comp = sys.modules["elecplace.compat"]
comp.to_text = lambda v: u"" if v is None else str(v)
comp.symbol_family_name = lambda s: s.family
comp.symbol_type_name = lambda s: s.type
import datamap as d
d.param_text = lambda el, name: (el.params.get(name) if el is not None else None)

NS = types.SimpleNamespace
room = NS(number=u"B1-105", name=u"Plant Room", level_name=u"B1", uid=u"r1",
          room=NS(params={u"Department": u"MEP"}))
msfd = NS(inst=NS(params={u"Mark": u"MSFD-12", u"System Name": u"SA-3"},
                  Symbol=NS(family=u"MEE-Duct Accessories with Access-Rectangular", type=u"MSFD")),
          elem_id=25109867, source=u"MEP-01")
rule = NS(label=u"Switched Fused Spur : MSFD", category_name=u"Electrical Fixtures")
pp_equip = NS(room=room, equipment=msfd, rule=rule)
pp_walls = NS(room=room, equipment=None, rule=rule)
outside = NS(room=NS(number=u"", name=u"(outside rooms)", level_name=u"B1", uid=u"x", room=None),
             equipment=msfd, rule=rule)

def test_parse():
    m = d.parse_mappings(u"""
        # comment line
        Comments = Serves {Equip:Mark} - Room {Room Number}
        EL_Room Name = {Room Name}
        EL_Status = NEW
    """)
    assert m == [(u"Comments", u"Serves {Equip:Mark} - Room {Room Number}"),
                 (u"EL_Room Name", u"{Room Name}"), (u"EL_Status", u"NEW")]
    for bad in (u"no equals sign", u" = {Room Name}", u"X = {Roomnumber}"):
        try:
            d.parse_mappings(bad); raise AssertionError(bad)
        except ValueError:
            pass

def test_render():
    miss = {}
    assert d.render(u"Serves {Equip:Mark} - Room {Room Number}", pp_equip, miss) == u"Serves MSFD-12 - Room B1-105"
    assert d.render(u"{Equip Type} on {Equip:System Name} ({Equip Model}) id {Equip Id}", pp_equip, miss) \
        == u"MSFD on SA-3 (MEP-01) id 25109867"
    assert d.render(u"{Room:Department} / {Room Level}", pp_equip, miss) == u"MEP / B1"
    assert d.render(u"NEW", pp_walls, miss) == u"NEW" and not miss
    # unavailable values are written empty and counted
    assert d.render(u"{Equip:Size}", pp_equip, miss) == u""
    assert d.render(u"Room {Room Number}", outside, miss) == u"Room"
    assert miss == {u"{Equip:Size}": 1, u"{Room Number}": 1}

if __name__ == "__main__":
    test_parse(); test_render(); print("all datamap tests passed")
