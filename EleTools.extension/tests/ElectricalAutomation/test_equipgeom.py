# -*- coding: utf-8 -*-
"""Checks EquipFrame side / offset math outside Revit using a tiny XYZ mock.
Run: python tests/test_equipgeom.py"""
import math, os, sys, types

class XYZ(object):
    def __init__(s, x, y, z): s.X, s.Y, s.Z = float(x), float(y), float(z)
    def Add(s, o): return XYZ(s.X + o.X, s.Y + o.Y, s.Z + o.Z)
    def Subtract(s, o): return XYZ(s.X - o.X, s.Y - o.Y, s.Z - o.Z)
    def Multiply(s, k): return XYZ(s.X * k, s.Y * k, s.Z * k)
    def Negate(s): return s.Multiply(-1)
    def DotProduct(s, o): return s.X * o.X + s.Y * o.Y + s.Z * o.Z
    def CrossProduct(s, o): return XYZ(s.Y*o.Z - s.Z*o.Y, s.Z*o.X - s.X*o.Z, s.X*o.Y - s.Y*o.X)
    def GetLength(s): return math.sqrt(s.DotProduct(s))
    def Normalize(s): return s.Multiply(1.0 / s.GetLength())
    def t(s): return tuple(round(v, 6) for v in (s.X, s.Y, s.Z))

# minimal module mocks so equipgeom imports outside Revit
db = types.SimpleNamespace(XYZ=XYZ)
sys.modules["System"] = types.ModuleType("System")
sys.modules["System.Collections"] = types.ModuleType("System.Collections")
gen = types.ModuleType("System.Collections.Generic"); gen.List = None
sys.modules["System.Collections.Generic"] = gen
ar = types.ModuleType("Autodesk"); arr = types.ModuleType("Autodesk.Revit"); arr.DB = db
sys.modules["Autodesk"] = ar; sys.modules["Autodesk.Revit"] = arr
comp = types.ModuleType("elecplace.compat"); comp.eid_int = lambda e: e; comp.element_name = str
pkg = types.ModuleType("elecplace"); pkg.__path__ = []
sys.modules["elecplace"] = pkg; sys.modules["elecplace.compat"] = comp
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "lib", "elecplace"))
import equipgeom as eg

def frame(facing, hand):
    # 1.0 wide (H), 0.6 deep (F), 0.3 high (Z) unit centred on origin (10, 20, 9)
    return eg.EquipFrame(XYZ(10, 20, 9), facing, hand, XYZ(0, 0, 1),
                         {"F": (-0.3, 0.3), "H": (-0.5, 0.5), "Z": (-0.15, 0.15)})

def test_axis_aligned():
    f = frame(XYZ(0, 1, 0), XYZ(1, 0, 0))
    p, n, a1, a2 = f.face_point("front", 0, 0)
    assert p.t() == (10, 20.3, 9) and n.t() == (0, 1, 0)
    # looking at the front (from +Y towards -Y) the viewer's right is -X
    p, n, a1, a2 = f.face_point("front", 0.2, 0.05)
    assert a1.t() == (-1, 0, 0) and p.t() == (9.8, 20.3, 9.05)
    p, n, a1, a2 = f.face_point("right", 0, 0)
    assert p.t() == (10.5, 20, 9) and n.t() == (1, 0, 0)
    p, n, a1, a2 = f.face_point("bottom", 0.1, 0.1)       # underside, towards back
    assert n.t() == (0, 0, -1) and p.t() == (10.1, 19.9, 8.85)
    p, n, a1, a2 = f.face_point("top", 0, 0.2)
    assert p.t() == (10, 20.2, 9.15)

def test_rotated_45():
    r = 1 / math.sqrt(2)
    f = frame(XYZ(-r, r, 0), XYZ(r, r, 0))                   # unit rotated 45 deg
    p, n, a1, a2 = f.face_point("front", 0, 0)
    assert abs(p.Subtract(XYZ(10, 20, 9)).GetLength() - 0.3) < 1e-9
    assert abs(n.DotProduct(XYZ(-r, r, 0)) - 1) < 1e-9
    assert abs(a1.DotProduct(n)) < 1e-9 and abs(a1.Z) < 1e-9   # offset 1 stays in face, horizontal

def test_xyz_offsets():
    f = frame(XYZ(0, 1, 0), XYZ(1, 0, 0))          # X = hand = +X, Y = front = +Y
    p, n, ax1, standoff = f.face_point_xyz("front", 0.2, 0.0, 0.05)
    assert p.t() == (10.2, 20.3, 9.05) and standoff == 0.0
    p, n, ax1, standoff = f.face_point_xyz("front", 0.0, 0.1, 0.0)   # Y on front = stand-off
    assert p.t() == (10, 20.3, 9) and abs(standoff - 0.1) < 1e-12
    p, n, ax1, standoff = f.face_point_xyz("bottom", 0.1, -0.1, 0.0)
    assert p.t() == (10.1, 19.9, 8.85)
    r = 1 / math.sqrt(2)                              # rotated unit: X follows its hand axis
    g = frame(XYZ(-r, r, 0), XYZ(r, r, 0))
    p, n, ax1, standoff = g.face_point_xyz("front", 1.0, 0, 0)
    c, _n, _a, _s = g.face_point_xyz("front", 0, 0, 0)
    d = p.Subtract(c)
    assert abs(d.X - r) < 1e-9 and abs(d.Y - r) < 1e-9

def test_legacy_templates_keep_position():
    f = frame(XYZ(0, 1, 0), XYZ(1, 0, 0))
    for side in ("front", "back", "right", "left", "top", "bottom"):
        old, _n, _a1, _a2 = f.face_point(side, 0.17, 0.06)
        new, _n, _a, standoff = f.face_point_xyz(side, *eg.legacy_offsets_to_xyz(side, 0.17, 0.06))
        assert old.t() == new.t() and abs(standoff) < 1e-12, side

def test_edge_shift_msfd():
    mm = 1 / 304.8
    lo, hi = -413 * mm, 413 * mm                 # MSFD 826 mm wide, centred
    assert round(eg.edge_shift(100 * mm, lo, hi) / mm, 6) == 513     # right end + 100
    assert round(eg.edge_shift(-100 * mm, lo, hi) / mm, 6) == -513   # left end - 100
    assert eg.edge_shift(0, lo, hi) == 0
    # off-centre family origin (e.g. origin at one flange): 0..826
    assert round(eg.edge_shift(100 * mm, 0, 826 * mm) / mm, 6) == 926
    assert round(eg.edge_shift(-100 * mm, 0, 826 * mm) / mm, 6) == -100

if __name__ == "__main__":
    test_axis_aligned(); test_rotated_45(); test_xyz_offsets(); test_legacy_templates_keep_position()
    test_edge_shift_msfd()
    print("all equipgeom tests passed")
