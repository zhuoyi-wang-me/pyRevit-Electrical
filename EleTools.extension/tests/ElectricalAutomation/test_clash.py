# -*- coding: utf-8 -*-
"""Clash index behaviour outside Revit (DB mocked). Run: python tests/test_clash.py"""
import os, sys, types
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_equipgeom            # installs the Revit / System mocks + XYZ   # noqa: F401
from test_equipgeom import XYZ
db = sys.modules["Autodesk.Revit"].DB
db.Transform = types.SimpleNamespace(Identity=None)
comp = sys.modules["elecplace.compat"]; comp.symbol_label = str; comp.to_text = str
sys.modules["elecplace.equipgeom"] = test_equipgeom.eg
import mathutil; sys.modules["elecplace.mathutil"] = mathutil
import clash as c

MM = 1 / 304.8
I = [(1, 0, 0), (0, 1, 0), (0, 0, 1)]

def spur_at(x_mm):
    # 86 x 86 socket box on a wall face (y = 0) at 1200 AFF, 40 deep into the room, +50 clearance
    return c.Box((x_mm * MM, 20 * MM, 1200 * MM), I, ((43 + 50) * MM, (20 + 50) * MM, (43 + 50) * MM))

def test_msfd_scenario():
    idx = c.ClashIndex()
    # MSFD 826 wide, 300 deep, 400 high, centred on the wall at 1100 mm
    msfd = c.Box((0, 0, 1100 * MM), I, (413 * MM, 150 * MM, 200 * MM))
    idx.add(c.Obstacle(u"MSFD [25109867]", ("link", 25109867), msfd.lo, msfd.hi, box=msfd))
    # FCU 1200 wide starting 1400 mm to the right
    fcu = c.Box((2000 * MM, 300 * MM, 2400 * MM), I, (600 * MM, 300 * MM, 150 * MM))
    idx.add(c.Obstacle(u"FCU [777]", (None, 777), fcu.lo, fcu.hi, box=fcu))

    assert idx.find(spur_at(100)) == u"MSFD [25109867]"         # on top of the damper
    assert idx.find(spur_at(513)) is None                       # 100 mm clear of its end
    # the damper itself is ignored when the fixture is hosted ON it
    assert idx.find(spur_at(100), ignore_key=("link", 25109867)) is None
    # slide search like the planner does: first clear spot to the right
    step = 50
    first = next(x for x in range(100, 1500, step) if idx.find(spur_at(x)) is None)
    assert first == 550, first            # needs x - 93 (half + clearance) >= 413 -> 550 on a 50 mm grid
    # new fixtures avoid each other
    idx.add_fixture(spur_at(550), u"new spur")
    assert idx.find(spur_at(600)) == u"new spur"

if __name__ == "__main__":
    test_msfd_scenario(); print("all clash tests passed")
