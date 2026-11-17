# -*- coding: utf-8 -*-
"""Run outside Revit:  python tests/test_mathutil.py"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "lib", "elecplace"))
import mathutil as m

def test_subtract():
    assert m.subtract_intervals((1, 9), [], 0.1) == [(1, 9)]
    assert m.subtract_intervals((1, 9), [(3, 5)], 0.1) == [(1, 3), (5, 9)]
    assert m.subtract_intervals((1, 9), [(0, 2), (8, 10)], 0.1) == [(2, 8)]
    assert m.subtract_intervals((1, 9), [(0, 10)], 0.1) == []
    assert m.subtract_intervals((1, 9), [(1.05, 8.95)], 0.1) == []   # slivers dropped

def test_distribute():
    slots = m.distribute([("A", 0, 10)], 2)
    assert [s[3] for s in slots] == [2.5, 7.5]
    slots = m.distribute([("A", 0, 4), ("B", 0, 4)], 2)
    assert (slots[0][0], slots[0][3]) == ("A", 2.0) and (slots[1][0], slots[1][3]) == ("B", 2.0)
    assert m.distribute([], 3) == [] and m.distribute([("A", 0, 1)], 0) == []
    for p, a, b, t in m.distribute([("A", 1, 2), ("B", 5, 5.5)], 7):
        assert a <= t <= b

def test_grid():
    g = m.GridIndex(0.5)
    g.add(0, 0, 0)
    assert g.near(0.3, 0, 0, 0.49)
    assert not g.near(0.6, 0, 0, 0.49)
    assert g.near(-0.4, -0.2, 0.1, 0.49)

def test_room_size():
    bands = m.parse_bands("10:2, 25:4, 50:6, *:8")
    assert m.band_count(bands, 8) == 2 and m.band_count(bands, 25) == 4
    assert m.band_count(bands, 30) == 6 and m.band_count(bands, 400) == 8
    assert m.band_count(m.parse_bands("10:2, 25:4"), 99) == 4      # no '*' -> last band
    for bad in ("25:4, 10:2", "*:3, 10:2", "abc", ""):
        try:
            m.parse_bands(bad); raise AssertionError("should fail: " + bad)
        except ValueError:
            pass
    assert m.count_by_spacing(10.0, 3.6) == 3 and m.count_by_spacing(7.2, 3.6) == 2
    assert m.count_by_spacing(0, 3.6) == 0
    assert m.clamp_qty(1, 2, 0) == 2 and m.clamp_qty(9, 0, 6) == 6 and m.clamp_qty(4) == 4

def test_nearest():
    # wall along +X from (0,0); usable 1..9; equipment at (5, 2)
    t, d, a, b = m.nearest_on_intervals(5, 2, 0, 0, 1, 0, [(1, 9)])
    assert (t, d) == (5, 2)
    t, d, a, b = m.nearest_on_intervals(5, 1, 0, 0, 1, 0, [(1, 4), (6, 9)])
    assert t in (4, 6)
    assert m.nearest_on_intervals(0, 0, 0, 0, 1, 0, []) is None

def test_obb():
    I = [(1, 0, 0), (0, 1, 0), (0, 0, 1)]
    # two 1x1x1 boxes: overlapping, touching, apart
    assert m.obb_overlap((0, 0, 0), I, (0.5, 0.5, 0.5), (0.9, 0, 0), I, (0.5, 0.5, 0.5))
    assert not m.obb_overlap((0, 0, 0), I, (0.5, 0.5, 0.5), (1.0, 0, 0), I, (0.5, 0.5, 0.5))
    assert not m.obb_overlap((0, 0, 0), I, (0.5, 0.5, 0.5), (3, 0, 0), I, (0.5, 0.5, 0.5))
    # 45 deg rotated box: AABBs overlap but boxes do not (corner gap)
    r = 2 ** -0.5
    R = [(r, r, 0), (-r, r, 0), (0, 0, 1)]
    assert not m.obb_overlap((0, 0, 0), I, (0.5, 0.5, 0.5), (1.2, 1.2, 0), R, (0.5, 0.5, 0.5))
    assert m.obb_overlap((0, 0, 0), I, (0.5, 0.5, 0.5), (0.7, 0.7, 0), R, (0.5, 0.5, 0.5))
    # long thin diagonal duct passing beside a socket
    duct = [(r, r, 0), (-r, r, 0), (0, 0, 1)]
    assert not m.obb_overlap((0, 0, 0), I, (0.1, 0.1, 0.1), (0, 2, 0), duct, (5, 0.3, 0.3))
    lo, hi = m.obb_aabb((0, 0, 0), R, (0.5, 0.5, 0.5))
    assert abs(hi[0] - r) < 1e-9 and abs(hi[2] - 0.5) < 1e-9
    assert m.nudge_offsets(1, 2, prefer=-1) == [0, -1, 1, -2, 2]

if __name__ == "__main__":
    test_obb()
    test_subtract(); test_distribute(); test_grid(); test_room_size(); test_nearest()
    print("all mathutil tests passed")
